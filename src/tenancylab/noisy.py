"""Noisy neighbor load generator.

One large tenant runs heavy reports in a loop on several threads while many small tenants do normal work
(get a task, list a project, search, add a comment). Latency for small tenants is measured end to end,
including time spent waiting for a database connection. Each pattern runs twice: without limits, and with
tenant-aware rate limits plus per-tenant concurrency caps.

In pooled and bridge, all tenants share one pool of SHARED_POOL connections (one database server). In
silo each tenant has its own pool, so the large tenant can only saturate its own database.
"""

from __future__ import annotations

import random
import statistics
import tempfile
import threading
import time
from pathlib import Path

from .limits import REPORT_COST, Policy, RateLimited, TenantLimiter
from .platform import Platform

SHARED_POOL = 4
SILO_POOL = 2
# One policy for every tenant: at most 2 requests in flight (half the shared pool) and 100 tokens per second.
# A report costs REPORT_COST tokens, so one tenant can run at most 10 reports per second.
MITIGATION = {"starter": Policy(rate=100, burst=50, concurrency=2, queue_timeout=1.0)}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def build(pattern: str, data_dir: Path, small: int, large_projects: int, large_tasks: int, seed: int) -> Platform:
    plat = Platform(data_dir, shared_pool=SHARED_POOL, silo_pool=SILO_POOL)
    plat.onboard("bigco", "Bigco Freight", pattern, "starter")
    plat.seed("bigco", large_projects, large_tasks, seed)
    for i in range(small):
        tid = f"small-{i:02d}"
        plat.onboard(tid, f"Small Shop {i}", pattern, "starter")
        plat.seed(tid, 3, 20, seed)
    return plat


def run_scenario(plat: Platform, mitigated: bool, duration: float, report_threads: int, seed: int) -> dict:
    limiter = TenantLimiter(MITIGATION, enabled=mitigated)
    stop = threading.Event()
    lat: list[float] = []
    report_ms: list[float] = []
    counters = {"small_ops": 0, "small_rejected": 0, "reports": 0, "reports_rejected": 0}
    lock = threading.Lock()
    small_ids = [t["id"] for t in plat.registry.all() if t["id"] != "bigco"]
    pick = {tid: (plat.repo(tid).list_tasks(limit=60), plat.repo(tid).list_projects()) for tid in small_ids}

    def big_worker():
        repo = plat.repo("bigco")
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                with limiter.admit("bigco", "starter", REPORT_COST):
                    repo.report()
            except RateLimited as exc:
                with lock:
                    counters["reports_rejected"] += 1
                stop.wait(min(exc.retry_after, 0.25))
                continue
            with lock:
                counters["reports"] += 1
                report_ms.append((time.perf_counter() - t0) * 1000)

    def small_worker(tid: str, rng: random.Random):
        repo = plat.repo(tid)
        tasks, projects = pick[tid]
        while not stop.is_set():
            stop.wait(rng.uniform(0.02, 0.04))
            op = rng.random()
            t0 = time.perf_counter()
            try:
                with limiter.admit(tid, "starter", 1):
                    if op < 0.4:
                        repo.get_task(rng.choice(tasks)["id"])
                    elif op < 0.7:
                        repo.list_tasks(project_id=rng.choice(projects)["id"], limit=20)
                    elif op < 0.85:
                        repo.search(rng.choice(["Fix", "billing", "cache"]), limit=10)
                    else:
                        repo.add_comment(rng.choice(tasks)["id"], "load test comment")
            except RateLimited:
                with lock:
                    counters["small_rejected"] += 1
                continue
            ms = (time.perf_counter() - t0) * 1000
            with lock:
                counters["small_ops"] += 1
                lat.append(ms)

    threads = [threading.Thread(target=big_worker, daemon=True) for _ in range(report_threads)]
    threads += [
        threading.Thread(target=small_worker, args=(tid, random.Random(f"{seed}:{tid}")), daemon=True)
        for tid in small_ids
    ]
    for t in threads:
        t.start()
    time.sleep(duration)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    return {
        "mitigated": mitigated,
        "small_p50_ms": round(percentile(lat, 50), 2),
        "small_p95_ms": round(percentile(lat, 95), 2),
        "small_p99_ms": round(percentile(lat, 99), 2),
        "small_mean_ms": round(statistics.fmean(lat), 2) if lat else 0.0,
        **counters,
        "report_p50_ms": round(percentile(report_ms, 50), 1),
        "duration_s": duration,
    }


def run(
    duration: float = 3.0,
    small: int = 50,
    large_projects: int = 40,
    large_tasks: int = 750,
    report_threads: int = 6,
    seed: int = 7,
    patterns=("pooled", "bridge", "silo"),
    data_dir=None,
) -> dict:
    results = []
    with tempfile.TemporaryDirectory(dir=data_dir) as tmp:
        for pattern in patterns:
            plat = build(pattern, Path(tmp) / pattern, small, large_projects, large_tasks, seed)
            try:
                plat.repo("bigco").report()  # warm caches so the first scenario is not penalized
                for mitigated in (False, True):
                    r = run_scenario(plat, mitigated, duration, report_threads, seed)
                    results.append({"pattern": pattern, **r})
            finally:
                plat.close()
    return {
        "config": {
            "duration_s": duration,
            "small_tenants": small,
            "large_tasks": large_projects * large_tasks,
            "report_threads": report_threads,
            "shared_pool": SHARED_POOL,
            "silo_pool": SILO_POOL,
            "seed": seed,
            "mitigation": {"concurrency_cap": 2, "rate_tokens_per_s": 100, "report_cost_tokens": REPORT_COST},
        },
        "results": results,
    }
