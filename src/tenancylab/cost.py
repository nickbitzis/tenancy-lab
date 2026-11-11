"""Monthly infrastructure and ops cost per tenant under each pattern.

Every number is an assumption, listed in ASSUMPTIONS and in the README. Change them and the curves move;
the shape (pooled flat and cheap, silo linear and expensive, bridge in between until catalog limits bite)
is the part that holds up.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

TENANT_COUNTS = (10, 100, 1_000, 10_000)


@dataclass(frozen=True)
class Assumptions:
    shared_instance_usd: float = 700.0  # managed Postgres, 8 vCPU / 32 GB, Multi-AZ, per month
    silo_instance_usd: float = 60.0  # smallest production-grade dedicated instance, per month
    tenants_per_pooled_instance: int = 2_000  # limited by CPU and working set, not catalog
    schemas_per_bridge_instance: int = 500  # catalog bloat, autovacuum and migration time cap this
    data_gb_per_tenant: float = 2.0
    storage_usd_per_gb: float = 0.115
    bridge_schema_overhead_gb: float = 0.05  # empty tables, indexes, catalog rows per schema
    silo_min_storage_gb: float = 20.0  # minimum volume per dedicated instance
    backup_ratio: float = 1.0  # backup storage as a share of primary
    backup_usd_per_gb: float = 0.095
    app_pods: int = 6  # each pod keeps a pool to every database it talks to
    pool_per_pod: int = 5
    connections_per_proxy: int = 5_000  # one PgBouncer or RDS Proxy unit
    proxy_usd: float = 90.0
    hourly_ops_usd: float = 120.0
    base_ops_hours: float = 20.0  # on-call, upgrades, monitoring: per month, any pattern
    ops_hours_per_instance: float = 0.5  # patching, alerts, capacity per instance per month
    migrations_per_month: float = 4.0
    migration_hours_per_unit: float = 0.002  # human time per schema or database touched (runner is automated)
    pooled_migration_hours: float = 0.5  # one big ALTER, planned carefully because it locks everyone


A = Assumptions()


def instances(pattern: str, n: int, a: Assumptions = A) -> int:
    if pattern == "pooled":
        return max(1, math.ceil(n / a.tenants_per_pooled_instance))
    if pattern == "bridge":
        return max(1, math.ceil(n / a.schemas_per_bridge_instance))
    if pattern == "silo":
        return n
    raise ValueError(pattern)


def connections(pattern: str, n: int, a: Assumptions = A) -> int:
    """Server-side connections the app fleet opens. Silo needs a pool per tenant database."""
    dbs = n if pattern == "silo" else instances(pattern, n, a)
    return a.app_pods * a.pool_per_pod * dbs


def monthly(pattern: str, n: int, a: Assumptions = A) -> dict:
    inst = instances(pattern, n, a)
    compute = inst * (a.silo_instance_usd if pattern == "silo" else a.shared_instance_usd)
    if pattern == "silo":
        gb = n * max(a.data_gb_per_tenant, a.silo_min_storage_gb)
    elif pattern == "bridge":
        gb = n * (a.data_gb_per_tenant + a.bridge_schema_overhead_gb)
    else:
        gb = n * a.data_gb_per_tenant
    storage = gb * a.storage_usd_per_gb
    backups = n * a.data_gb_per_tenant * a.backup_ratio * a.backup_usd_per_gb
    conns = connections(pattern, n, a)
    # Proxies are only needed once connections pile up; silo at scale cannot run without them.
    proxies = math.ceil(conns / a.connections_per_proxy) if conns > a.connections_per_proxy else 0
    proxy = proxies * a.proxy_usd
    if pattern == "pooled":
        mig_hours = a.migrations_per_month * a.pooled_migration_hours * inst
    else:
        mig_hours = a.migrations_per_month * a.migration_hours_per_unit * n
    ops_hours = a.base_ops_hours + a.ops_hours_per_instance * inst + mig_hours
    ops = ops_hours * a.hourly_ops_usd
    total = compute + storage + backups + proxy + ops
    return {
        "pattern": pattern,
        "tenants": n,
        "instances": inst,
        "connections": conns,
        "proxies": proxies,
        "compute_usd": round(compute, 2),
        "storage_usd": round(storage, 2),
        "backup_usd": round(backups, 2),
        "proxy_usd": round(proxy, 2),
        "ops_hours": round(ops_hours, 2),
        "ops_usd": round(ops, 2),
        "total_usd": round(total, 2),
        "per_tenant_usd": round(total / n, 2),
    }


def table(counts=TENANT_COUNTS, a: Assumptions = A) -> list[dict]:
    return [monthly(p, n, a) for p in ("pooled", "bridge", "silo") for n in counts]


def model(a: Assumptions = A) -> dict:
    return {"assumptions": asdict(a), "rows": table(a=a)}
