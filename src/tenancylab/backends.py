"""Three storage backends behind one TenantRepo.

pooled: one database, shared tables, tenant_id on every row. Two layers enforce the scope: the query layer
        always adds tenant_id = ?, and each connection has TEMP views filtered by current_tenant() plus TEMP
        triggers that reject writes for any other tenant. That pair is the SQLite stand-in for Postgres
        row level security with SET app.tenant_id.
bridge: one server (one database file and a shared pool of connections), one schema per tenant. SQLite has
        no CREATE SCHEMA, so a schema is a table-name namespace (t_acme__tasks) in the shared catalog. That
        keeps the real bridge costs: one buffer cache and connection limit for everyone, a catalog that
        grows by 11 objects per tenant, and a migration per schema. (ATTACH per tenant was tried first; the
        10-database ATTACH limit forced attach churn that cleared the page cache on every switch, which is
        a SQLite artifact, not a property of the pattern.)
silo:   one database per tenant, each with its own connection pool.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .db import Pool, connect
from .repo import PlantedBugRepo, TenantRepo
from .schema import TABLES, current_version, migrate

TENANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
PATTERNS = ("pooled", "bridge", "silo")


class UnknownTenant(KeyError):
    pass


def check_tenant_id(tid: str) -> str:
    if not TENANT_ID.match(tid):
        raise ValueError(f"invalid tenant id {tid!r}")
    return tid


def _remove_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


class Backend:
    pattern = "base"

    def __init__(self, data_dir: Path, pool_size: int = 4):
        self.dir = Path(data_dir) / self.pattern
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pool_size = pool_size

    # hooks used by TenantRepo
    def checkout(self, tid: str):  # pragma: no cover - abstract
        raise NotImplementedError

    def table_ref(self, tid: str, name: str, write: bool) -> str:
        return name

    def scope(self, tid: str, alias: str = "") -> tuple[str, list]:
        return "1 = 1", []

    def repo(self, tid: str) -> TenantRepo:
        return TenantRepo(self, tid)

    # lifecycle
    def create_tenant(self, tid: str, version: int) -> None:
        raise NotImplementedError

    def drop_tenant(self, tid: str) -> None:
        raise NotImplementedError

    def has_storage(self, tid: str) -> bool:
        raise NotImplementedError

    # migrations: a unit is what one migration step touches
    def migration_units(self, tenants: list[str]) -> list[str]:
        return list(tenants)

    def migrate_unit(self, unit: str, target: int, fail: bool = False) -> int:
        raise NotImplementedError

    def version(self, tid: str) -> int:
        raise NotImplementedError

    def pool_wait_seconds(self) -> float:
        return 0.0

    def close(self) -> None:
        pass


class PooledBackend(Backend):
    pattern = "pooled"

    def __init__(self, data_dir: Path, pool_size: int = 4, planted_bug: bool = False, initial_version: int = 4):
        super().__init__(data_dir, pool_size)
        self.path = self.dir / "pooled.db"
        self.planted_bug = planted_bug
        boot = connect(str(self.path))
        migrate(boot, initial_version)
        boot.close()
        self._holders: dict[int, dict] = {}
        self.pool = Pool(self._factory, pool_size, "pooled")

    def _factory(self) -> sqlite3.Connection:
        conn = connect(str(self.path))
        holder: dict = {"tid": None}
        conn.create_function("current_tenant", 0, lambda: holder["tid"])
        for t in TABLES:
            conn.execute(
                f"CREATE TEMP VIEW v_{t} AS SELECT * FROM main.{t} WHERE tenant_id = (SELECT current_tenant())"
            )
            for event, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                conn.execute(
                    f"CREATE TEMP TRIGGER guard_{t}_{event.lower()} BEFORE {event} ON main.{t} "
                    f"WHEN {row}.tenant_id IS NOT current_tenant() OR current_tenant() IS NULL "
                    "BEGIN SELECT RAISE(ABORT, 'tenant guard: row belongs to another tenant'); END"
                )
        self._holders[id(conn)] = holder
        return conn

    @contextmanager
    def checkout(self, tid: str) -> Iterator[sqlite3.Connection]:
        with self.pool.connection() as conn:
            holder = self._holders[id(conn)]
            holder["tid"] = tid
            try:
                yield conn
            finally:
                holder["tid"] = None

    def table_ref(self, tid: str, name: str, write: bool) -> str:
        return f"main.{name}" if write else f"temp.v_{name}"

    def scope(self, tid: str, alias: str = "") -> tuple[str, list]:
        return f"{alias}tenant_id = ?", [tid]

    def repo(self, tid: str) -> TenantRepo:
        return PlantedBugRepo(self, tid) if self.planted_bug else TenantRepo(self, tid)

    def create_tenant(self, tid: str, version: int) -> None:
        check_tenant_id(tid)  # no DDL: a new tenant is just a new tenant_id value

    def drop_tenant(self, tid: str) -> None:
        self.repo(tid).delete_all()

    def has_storage(self, tid: str) -> bool:
        return any(self.repo(tid).counts().values())

    def migration_units(self, tenants: list[str]) -> list[str]:
        return ["shared"]  # one ALTER on the shared tables covers every tenant at once

    def migrate_unit(self, unit: str, target: int, fail: bool = False) -> int:
        with self.pool.connection() as conn:
            return migrate(conn, target, "", fail)

    def version(self, tid: str = "") -> int:
        with self.pool.connection() as conn:
            return current_version(conn)

    def pool_wait_seconds(self) -> float:
        return self.pool.wait_seconds

    def close(self) -> None:
        self.pool.close()


class BridgeBackend(Backend):
    pattern = "bridge"

    def __init__(self, data_dir: Path, pool_size: int = 4):
        super().__init__(data_dir, pool_size)
        self.path = self.dir / "server.db"
        self.pool = Pool(lambda: connect(str(self.path)), pool_size, "bridge")

    @staticmethod
    def prefix(tid: str) -> str:
        return "t_" + check_tenant_id(tid).replace("-", "_") + "__"

    @contextmanager
    def checkout(self, tid: str) -> Iterator[sqlite3.Connection]:
        with self.pool.connection() as conn:
            yield conn

    def table_ref(self, tid: str, name: str, write: bool) -> str:
        return self.prefix(tid) + name

    def create_tenant(self, tid: str, version: int) -> None:
        with self.checkout(tid) as conn:
            migrate(conn, version, self.prefix(tid))

    def _objects(self, conn: sqlite3.Connection, tid: str) -> list[str]:
        p = self.prefix(tid).replace("_", "\\_")
        sql = "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ? ESCAPE '\\'"
        return [r[0] for r in conn.execute(sql, (p + "%",))]

    def drop_tenant(self, tid: str) -> None:
        with self.checkout(tid) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for name in self._objects(conn, tid):
                conn.execute(f"DROP TABLE {name}")  # indexes go with their tables
            conn.execute("COMMIT")

    def has_storage(self, tid: str) -> bool:
        with self.checkout(tid) as conn:
            return bool(self._objects(conn, tid))

    def catalog_objects(self) -> int:
        with self.pool.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]

    def migrate_unit(self, unit: str, target: int, fail: bool = False) -> int:
        with self.checkout(unit) as conn:
            return migrate(conn, target, self.prefix(unit), fail)

    def version(self, tid: str) -> int:
        with self.checkout(tid) as conn:
            return current_version(conn, self.prefix(tid))

    def pool_wait_seconds(self) -> float:
        return self.pool.wait_seconds

    def close(self) -> None:
        self.pool.close()


class SiloBackend(Backend):
    pattern = "silo"

    def __init__(self, data_dir: Path, pool_size: int = 2):
        super().__init__(data_dir, pool_size)
        self.pools: dict[str, Pool] = {}
        self._lock = threading.Lock()

    def file(self, tid: str) -> Path:
        return self.dir / f"{check_tenant_id(tid)}.db"

    def _pool(self, tid: str) -> Pool:
        with self._lock:
            pool = self.pools.get(tid)
            if pool is None:
                if not self.file(tid).exists():
                    raise UnknownTenant(tid)
                path = str(self.file(tid))
                pool = self.pools[tid] = Pool(lambda: connect(path), self.pool_size, f"silo:{tid}")
            return pool

    @contextmanager
    def checkout(self, tid: str) -> Iterator[sqlite3.Connection]:
        with self._pool(tid).connection() as conn:
            yield conn

    def create_tenant(self, tid: str, version: int) -> None:
        conn = connect(str(self.file(tid)))
        migrate(conn, version)
        conn.close()

    def drop_tenant(self, tid: str) -> None:
        with self._lock:
            pool = self.pools.pop(tid, None)
        if pool:
            pool.close()
        _remove_db(self.file(tid))

    def has_storage(self, tid: str) -> bool:
        return self.file(tid).exists()

    def migrate_unit(self, unit: str, target: int, fail: bool = False) -> int:
        with self.checkout(unit) as conn:
            return migrate(conn, target, "", fail)

    def version(self, tid: str) -> int:
        with self.checkout(tid) as conn:
            return current_version(conn)

    def pool_wait_seconds(self) -> float:
        return sum(p.wait_seconds for p in list(self.pools.values()))

    def close(self) -> None:
        for p in list(self.pools.values()):
            p.close()
        self.pools.clear()


def make_backends(
    data_dir: Path, planted_bug: bool = False, shared_pool: int = 4, silo_pool: int = 2, initial_version: int = 4
) -> dict[str, Backend]:
    os.makedirs(data_dir, exist_ok=True)
    return {
        "pooled": PooledBackend(data_dir, shared_pool, planted_bug, initial_version),
        "bridge": BridgeBackend(data_dir, shared_pool),
        "silo": SiloBackend(data_dir, silo_pool),
    }
