"""Operations benchmarks and the demo dataset.

migration_effort: onboard N tenants per pattern on schema v1, run a rolling migration to the latest version
with one injected failure partway through, resume it, and count what each pattern had to touch.
seed_demo: the dataset the web UI shows: a hybrid fleet with pooled, bridge and silo tenants.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from .backends import PATTERNS
from .platform import Platform
from .schema import LATEST


def migration_effort(n: int = 200, seed: int = 7) -> dict:
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for pattern in PATTERNS:
            plat = Platform(Path(tmp) / pattern, initial_version=1)
            try:
                for i in range(n):
                    tid = f"t{i:04d}"
                    plat.onboard(tid, f"Tenant {i}", pattern, version=1)
                    plat.seed(tid, 2, 10, seed)
                run_id = plat.start_migration(pattern, LATEST)
                units = [
                    s["unit"]
                    for s in plat.registry.q(
                        "SELECT unit FROM migration_steps WHERE run_id = ? ORDER BY rowid", (run_id,)
                    )
                ]
                fail_at = units[len(units) // 2]
                t0 = time.perf_counter()
                first = plat.run_migration(run_id, fail_units={fail_at})
                final = plat.run_migration(run_id)  # resume: skips done units, retries the failed one
                wall = (time.perf_counter() - t0) * 1000
                versions = {plat.version(t["id"]) for t in plat.registry.all()}
                out.append(
                    {
                        "pattern": pattern,
                        "tenants": n,
                        "units": final["units"],
                        "ddl_statements": final["ddl_statements"],
                        "migration_ms": final["ms"],
                        "wall_ms": round(wall, 1),
                        "failed_at": fail_at,
                        "done_before_failure": first["done"],
                        "resumed_from": first["done"],
                        "final_status": final["status"],
                        "all_on_latest": versions == {LATEST},
                        "tenants_blocked_per_unit": n if pattern == "pooled" else 1,
                    }
                )
            finally:
                plat.close()
    return {"latest": LATEST, "results": out}


DEMO = [
    # id, name, pattern, tier, projects, tasks per project
    ("bluefin-labs", "Bluefin Labs", "pooled", "starter", 3, 25),
    ("cedar-analytics", "Cedar Analytics", "pooled", "starter", 4, 30),
    ("dunmore-print", "Dunmore Print", "pooled", "starter", 2, 15),
    ("elkhorn-games", "Elkhorn Games", "pooled", "starter", 5, 40),
    ("fernway-travel", "Fernway Travel", "pooled", "starter", 3, 20),
    ("goldcrest-legal", "Goldcrest Legal", "pooled", "starter", 2, 30),
    ("harbor-bikes", "Harbor Bikes", "pooled", "starter", 6, 50),
    ("ironleaf-design", "Ironleaf Design", "pooled", "starter", 2, 10),
    ("juniper-clinics", "Juniper Clinics", "bridge", "growth", 10, 80),
    ("kestrelway-logistics", "Kestrelway Logistics", "bridge", "growth", 12, 90),
    ("quillfeather-bank", "Quillfeather Bank", "silo", "enterprise", 20, 150),
    ("northvale-insure", "Northvale Insure", "silo", "enterprise", 25, 120),
]


def seed_demo(plat: Platform) -> None:
    """Build the demo fleet on schema v2, roll it to the latest schema with one failure and a resume, then
    upgrade one pooled tenant to silo. Idempotent: does nothing if tenants already exist."""
    if plat.registry.all(include_deleted=True):
        return
    for tid, name, pattern, tier, projects, tasks in DEMO:
        plat.onboard(tid, name, pattern, tier, version=2)
        plat.seed(tid, projects, tasks)
    for pattern in PATTERNS:
        run_id = plat.start_migration(pattern, LATEST)
        fail = {"northvale-insure"} if pattern == "silo" else {"shared"} if pattern == "pooled" else set()
        plat.run_migration(run_id, fail_units=fail)
        plat.run_migration(run_id)
    plat.move("harbor-bikes", "silo")
    plat.onboard("tidewater-coffee", "Tidewater Coffee", "pooled", "starter")
    plat.seed("tidewater-coffee", 2, 12)
    plat.offboard("tidewater-coffee")
