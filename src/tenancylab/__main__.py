"""Command line: serve, redteam, noisy, migrations, cost, token, bench."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import cost, noisy, ops, redteam
from .backends import PATTERNS

RESULTS = Path(os.environ.get("TENANCYLAB_RESULTS", "results"))


def _write(name: str, data: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / name).write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {RESULTS / name}")


def cmd_serve(a) -> None:
    import uvicorn

    from .app import Settings, create_app
    from .platform import Platform
    from .schema import LATEST

    settings = Settings(data_dir=Path(a.data))
    fresh = not (settings.data_dir / "control.db").exists()
    plat = Platform(settings.data_dir, planted_bug=settings.planted_bug, initial_version=2 if fresh else LATEST)
    if fresh and not a.no_demo:
        ops.seed_demo(plat)
    app = create_app(settings, plat)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


def cmd_redteam(a) -> int:
    logging.getLogger("tenancylab").disabled = True
    patterns = PATTERNS if a.backend == "all" else (a.backend,)
    runs = [redteam.run_backend(p, planted_bug=a.planted_bug) for p in patterns]
    for r in runs:
        tag = " (planted bug ON)" if r["planted_bug"] else ""
        print(f"{r['backend']:<7}{tag}: {r['attacks']} attacks, {r['leaks']} leaks {r['leaked_attacks'] or ''}")
    if a.out:
        _write(
            a.out,
            {"canary": redteam.CANARY, "runs": runs + ([] if a.planted_bug else [redteam.run_backend("pooled", True)])},
        )
    leaks = sum(r["leaks"] for r in runs)
    if a.expect_leaks:
        if leaks == 0:
            print("FAIL: the planted bug was not caught")
            return 1
        print("OK: the suite caught the planted bug")
        return 0
    if leaks:
        print("FAIL: cross-tenant leak found")
        return 1
    print("OK: zero leaks")
    return 0


def cmd_noisy(a) -> None:
    r = noisy.run(duration=a.duration, small=a.small)
    for x in r["results"]:
        mode = "limits on " if x["mitigated"] else "limits off"
        print(
            f"{x['pattern']:<7} {mode}  small p50 {x['small_p50_ms']:>8.2f} ms  p99 {x['small_p99_ms']:>8.2f} ms  "
            f"small ops {x['small_ops']:>5}  reports {x['reports']:>4}  reports rejected {x['reports_rejected']}"
        )
    _write("noisy_neighbor.json", r)


def cmd_migrations(a) -> None:
    r = ops.migration_effort(a.tenants)
    for x in r["results"]:
        print(
            f"{x['pattern']:<7} units {x['units']:>4}  DDL {x['ddl_statements']:>5}  {x['migration_ms']:>8.1f} ms  "
            f"failed at {x['failed_at']}, resumed, status {x['final_status']}, all latest {x['all_on_latest']}"
        )
    _write("migration_effort.json", r)


def cmd_cost(a) -> None:
    for row in cost.table():
        print(
            f"{row['pattern']:<7} {row['tenants']:>6} tenants  ${row['total_usd']:>12,.0f}/mo  "
            f"${row['per_tenant_usd']:>9,.2f} per tenant"
        )


def cmd_token(a) -> None:
    from .auth import issue_token

    secret = os.environ.get("TENANCYLAB_SECRET")
    if not secret:
        sys.exit("set TENANCYLAB_SECRET to the same value the server uses")
    print(issue_token(secret, a.tenant, a.sub, a.role))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tenancylab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8875")))
    s.add_argument("--data", default=os.environ.get("TENANCYLAB_DATA", "data"))
    s.add_argument("--no-demo", action="store_true")
    r = sub.add_parser("redteam")
    r.add_argument("--backend", default="all", choices=["all", *PATTERNS])
    r.add_argument("--planted-bug", action="store_true")
    r.add_argument("--expect-leaks", action="store_true", help="exit 1 unless a leak is found")
    r.add_argument("--out", help="write results to results/<name>")
    n = sub.add_parser("noisy")
    n.add_argument("--duration", type=float, default=3.0)
    n.add_argument("--small", type=int, default=50)
    m = sub.add_parser("migrations")
    m.add_argument("--tenants", type=int, default=200)
    sub.add_parser("cost")
    t = sub.add_parser("token")
    t.add_argument("--tenant", required=True)
    t.add_argument("--sub", default="usr_demo")
    t.add_argument("--role", default="member")
    a = ap.parse_args(argv)
    handlers = {
        "serve": cmd_serve,
        "redteam": cmd_redteam,
        "noisy": cmd_noisy,
        "migrations": cmd_migrations,
        "cost": cmd_cost,
        "token": cmd_token,
    }
    code = handlers[a.cmd](a)
    sys.exit(code or 0)


if __name__ == "__main__":
    main()
