"""The control plane: routes each tenant to its backend and runs tenant operations.

Operations: onboarding, export, offboarding with verification, single-tenant backup and restore, rolling
schema migrations with resume, and moving a tenant from pooled to silo with checksum verification.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .backends import PATTERNS, Backend, UnknownTenant, check_tenant_id, make_backends
from .registry import Registry
from .repo import TenantRepo, now
from .schema import LATEST
from .seed import generate

TIERS = ("starter", "growth", "enterprise")


class TenantUnavailable(Exception):
    pass


class MoveFailed(Exception):
    def __init__(self, report: dict):
        super().__init__(report.get("error", "move failed"))
        self.report = report


class Jobs:
    """Background jobs. A job records the tenant that created it; reading it as any other tenant is a miss."""

    def __init__(self, workers: int = 2):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def submit(self, tenant_id: str, kind: str, fn: Callable[[], dict]) -> dict:
        job = {
            "id": "job_" + uuid.uuid4().hex[:12],
            "tenant_id": tenant_id,
            "kind": kind,
            "status": "queued",
            "created_at": now(),
            "result": None,
        }
        with self.lock:
            self.jobs[job["id"]] = job

        def run():
            job["status"] = "running"
            try:
                job["result"] = fn()
                job["status"] = "done"
            except Exception as exc:  # job errors are reported, not raised
                job["status"], job["result"] = "failed", {"error": str(exc)}

        job["future"] = self.pool.submit(run)
        return self.public(job)

    def get(self, tenant_id: str, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if job is None or job["tenant_id"] != tenant_id:
            return None
        return self.public(job)

    def wait(self, job_id: str, timeout: float = 10) -> None:
        self.jobs[job_id]["future"].result(timeout=timeout)

    @staticmethod
    def public(job: dict) -> dict:
        return {k: v for k, v in job.items() if k != "future"}

    def close(self) -> None:
        self.pool.shutdown(wait=True)


class Platform:
    def __init__(
        self,
        data_dir: str | Path,
        planted_bug: bool = False,
        shared_pool: int = 4,
        silo_pool: int = 2,
        initial_version: int = LATEST,
    ):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(self.dir)
        self.backends: dict[str, Backend] = make_backends(
            self.dir, planted_bug, shared_pool, silo_pool, initial_version
        )
        self.jobs = Jobs()
        self.planted_bug = planted_bug

    # routing ---------------------------------------------------------------
    def tenant(self, tid: str) -> dict:
        t = self.registry.get(tid)
        if t is None or t["status"] == "deleted":
            raise UnknownTenant(tid)
        return t

    def repo(self, tid: str, allow_unavailable: bool = False) -> TenantRepo:
        t = self.tenant(tid)
        if t["status"] != "active" and not allow_unavailable:
            raise TenantUnavailable(f"{tid} is {t['status']}")
        return self.backends[t["pattern"]].repo(tid)

    def version(self, tid: str) -> int:
        return self.backends[self.tenant(tid)["pattern"]].version(tid)

    # onboarding --------------------------------------------------------------
    def onboard(
        self,
        tid: str,
        name: str,
        pattern: str = "pooled",
        tier: str = "starter",
        version: int = LATEST,
        admin_email: str | None = None,
    ) -> dict:
        check_tenant_id(tid)
        if pattern not in PATTERNS or tier not in TIERS:
            raise ValueError("unknown pattern or tier")
        existing = self.registry.get(tid)
        if existing and existing["status"] != "deleted":
            raise ValueError(f"tenant {tid} exists")
        if existing:
            self.registry.remove(tid)
        t0 = time.perf_counter()
        self.backends[pattern].create_tenant(tid, version)
        self.registry.add(tid, name, pattern, tier)
        if admin_email:
            self.backends[pattern].repo(tid).create_user(admin_email, "Admin", "admin")
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self.registry.event("onboard", tid, pattern=pattern, tier=tier, ms=ms)
        return {**self.tenant(tid), "provision_ms": ms}

    def seed(self, tid: str, projects: int, tasks_per_project: int, seed: int = 7) -> int:
        return self.repo(tid).import_rows(generate(tid, projects, tasks_per_project, seed))

    def tenants(self) -> list[dict]:
        out = []
        for t in self.registry.all():
            try:
                repo = self.repo(t["id"], allow_unavailable=True)
                counts = repo.counts()
                version = self.version(t["id"])
            except Exception:  # a tenant mid-move or broken should not hide the others
                counts, version = {}, None
            out.append({**t, "rows": sum(counts.values()), "counts": counts, "schema_version": version})
        return out

    # export, offboarding -----------------------------------------------------
    def export(self, tid: str) -> dict:
        repo = self.repo(tid, allow_unavailable=True)
        t = self.tenant(tid)
        tables = repo.export()
        return {
            "tenant": t,
            "schema_version": self.version(tid),
            "exported_at": now(),
            "counts": {k: len(v) for k, v in tables.items()},
            "checksums": repo.checksums(),
            "tables": tables,
        }

    def offboard(self, tid: str) -> dict:
        t = self.tenant(tid)
        backend = self.backends[t["pattern"]]
        self.registry.set(tid, status="offboarding")
        data = self.export(tid)
        exports = self.dir / "exports"
        exports.mkdir(exist_ok=True)
        path = exports / f"{tid}-{int(time.time())}.json.gz"
        with gzip.open(path, "wt") as f:
            json.dump(data, f, default=str)
        backend.drop_tenant(tid)
        remaining = sum(backend.repo(tid).counts().values()) if t["pattern"] == "pooled" else 0
        storage = t["pattern"] != "pooled" and backend.has_storage(tid)
        verified = remaining == 0 and not storage
        self.registry.set(tid, status="deleted" if verified else "offboard_failed")
        report = {
            "tenant_id": tid,
            "pattern": t["pattern"],
            "export_path": str(path),
            "exported_rows": sum(data["counts"].values()),
            "export_checksum": data["checksums"]["all"],
            "rows_remaining": remaining,
            "storage_exists": storage,
            "verified": verified,
        }
        self.registry.event("offboard", tid, **{k: v for k, v in report.items() if k != "export_path"})
        return report

    # backup and restore ------------------------------------------------------
    def backup(self, tid: str) -> dict:
        data = self.export(tid)
        bdir = self.dir / "backups"
        bdir.mkdir(exist_ok=True)
        path = bdir / f"{tid}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:4]}.json.gz"
        with gzip.open(path, "wt") as f:
            json.dump(data, f, default=str)
        self.registry.event("backup", tid, checksum=data["checksums"]["all"])
        return {"tenant_id": tid, "path": str(path), "checksum": data["checksums"]["all"], "counts": data["counts"]}

    def restore(self, tid: str, path: str | Path) -> dict:
        with gzip.open(path, "rt") as f:
            data = json.load(f)
        if data["tenant"]["id"] != tid:
            raise ValueError("backup belongs to another tenant")
        repo = self.repo(tid, allow_unavailable=True)
        self.registry.set(tid, status="restoring")
        try:
            repo.delete_all()
            repo.import_rows(data["tables"])
            after = repo.checksums()
        finally:
            self.registry.set(tid, status="active")
        ok = after["all"] == data["checksums"]["all"]
        self.registry.event("restore", tid, verified=ok)
        return {"tenant_id": tid, "verified": ok, "checksum": after["all"], "expected": data["checksums"]["all"]}

    # pooled to silo ----------------------------------------------------------
    def move(self, tid: str, to: str = "silo", tamper: Callable[[dict], None] | None = None) -> dict:
        """Move a tenant between patterns. Writes are blocked (status 'moving') for the copy window.

        tamper lets a test corrupt the copy in flight to show the checksum gate aborting the move.
        """
        t = self.tenant(tid)
        src_b, dst_b = self.backends[t["pattern"]], self.backends[to]
        if t["pattern"] == to:
            raise ValueError("tenant already uses that pattern")
        t0 = time.perf_counter()
        self.registry.set(tid, status="moving")
        src = src_b.repo(tid)
        version = src_b.version(tid)
        data = src.export()
        src_sums = src.checksums()
        report = {
            "tenant_id": tid,
            "from": t["pattern"],
            "to": to,
            "rows": sum(len(v) for v in data.values()),
            "source_checksums": src_sums,
        }
        try:
            dst_b.create_tenant(tid, version)
            if tamper:
                tamper(data)
            dst_b.repo(tid).import_rows(data)
            dst_sums = dst_b.repo(tid).checksums()
            report["target_checksums"] = dst_sums
            mismatched = [k for k in src_sums if src_sums[k] != dst_sums.get(k)]
            if mismatched:
                raise MoveFailed({**report, "error": f"checksum mismatch in {mismatched}", "mismatched": mismatched})
        except Exception as exc:
            dst_b.drop_tenant(tid)
            self.registry.set(tid, status="active")
            rep = exc.report if isinstance(exc, MoveFailed) else {**report, "error": str(exc)}
            rep["moved"] = False
            self.registry.event("move_aborted", tid, error=rep["error"])
            raise MoveFailed(rep) from exc
        tier = "enterprise" if to == "silo" else t["tier"]
        self.registry.set(tid, pattern=to, tier=tier)
        src_b.drop_tenant(tid)
        left = sum(src_b.repo(tid).counts().values()) if t["pattern"] == "pooled" else int(src_b.has_storage(tid))
        self.registry.set(tid, status="active")
        report |= {
            "moved": True,
            "source_rows_remaining": left,
            "verified": left == 0,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        self.registry.event(
            "move", tid, **{k: v for k, v in report.items() if not k.endswith("checksums")}, checksum=src_sums["all"]
        )
        return report

    # migrations --------------------------------------------------------------
    def start_migration(self, pattern: str, target: int = LATEST) -> int:
        if target > LATEST:
            raise ValueError("unknown target version")
        tenants = [t["id"] for t in self.registry.all(pattern)]
        units = self.backends[pattern].migration_units(tenants)
        run_id = self.registry.x(
            "INSERT INTO migration_runs (pattern, target, status, started_at) VALUES (?,?,?,?)",
            (pattern, target, "pending", now()),
        )
        for u in units:
            n = len(tenants) if u == "shared" else 1
            self.registry.x(
                "INSERT INTO migration_steps (run_id, unit, tenants, status) VALUES (?,?,?,?)",
                (run_id, u, n, "pending"),
            )
        return run_id

    def run_migration(self, run_id: int, fail_units: set[str] | None = None, max_units: int | None = None) -> dict:
        """Process pending and failed steps in order. Stops at the first failure; calling again resumes.

        fail_units injects a failure on those units (for the demo and tests). max_units stops early to
        simulate the runner being killed partway through.
        """
        fail_units = fail_units or set()
        run = self.registry.q("SELECT * FROM migration_runs WHERE id = ?", (run_id,))[0]
        backend = self.backends[run["pattern"]]
        self.registry.x("UPDATE migration_runs SET status = 'running', attempts = attempts + 1 WHERE id = ?", (run_id,))
        steps = self.registry.q(
            "SELECT * FROM migration_steps WHERE run_id = ? AND status != 'done' ORDER BY rowid", (run_id,)
        )
        done_now = 0
        for step in steps:
            if max_units is not None and done_now >= max_units:
                self.registry.x("UPDATE migration_runs SET status = 'paused' WHERE id = ?", (run_id,))
                return self.migration_status(run_id)
            t0 = time.perf_counter()
            try:
                ddl = backend.migrate_unit(step["unit"], run["target"], fail=step["unit"] in fail_units)
            except Exception as exc:  # MigrationFailed or a real DDL error
                self.registry.x(
                    "UPDATE migration_steps SET status='failed', attempts=attempts+1, error=? "
                    "WHERE run_id=? AND unit=?",
                    (str(exc), run_id, step["unit"]),
                )
                self.registry.x("UPDATE migration_runs SET status = 'failed' WHERE id = ?", (run_id,))
                return self.migration_status(run_id)
            ms = (time.perf_counter() - t0) * 1000
            self.registry.x(
                "UPDATE migration_steps SET status='done', attempts=attempts+1, error=NULL, ddl=?, ms=? "
                "WHERE run_id=? AND unit=?",
                (ddl, ms, run_id, step["unit"]),
            )
            done_now += 1
        self.registry.x("UPDATE migration_runs SET status='done', finished_at=? WHERE id=?", (now(), run_id))
        return self.migration_status(run_id)

    def migration_status(self, run_id: int) -> dict:
        run = self.registry.q("SELECT * FROM migration_runs WHERE id = ?", (run_id,))[0]
        steps = self.registry.q("SELECT * FROM migration_steps WHERE run_id = ? ORDER BY rowid", (run_id,))
        by = {s: sum(1 for x in steps if x["status"] == s) for s in ("done", "failed", "pending")}
        return {
            **run,
            "units": len(steps),
            "tenants": sum(s["tenants"] for s in steps),
            **by,
            "progress": round(by["done"] / len(steps), 3) if steps else 1.0,
            "ddl_statements": sum(s["ddl"] for s in steps),
            "ms": round(sum(s["ms"] for s in steps), 1),
            "step_attempts": sum(s["attempts"] for s in steps),
            "failed_units": [{"unit": s["unit"], "error": s["error"]} for s in steps if s["status"] == "failed"],
            "recovered_units": [s["unit"] for s in steps if s["status"] == "done" and s["attempts"] > 1],
        }

    def migrations(self) -> list[dict]:
        ids = [r["id"] for r in self.registry.q("SELECT id FROM migration_runs ORDER BY id")]
        return [self.migration_status(i) for i in ids]

    def close(self) -> None:
        self.jobs.close()
        for b in self.backends.values():
            b.close()
        self.registry.close()
