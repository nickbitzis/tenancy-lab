"""The control plane catalog: which tenants exist, where each one lives, and migration run state."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .db import connect
from .repo import now

DDL = [
    """CREATE TABLE IF NOT EXISTS tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, pattern TEXT NOT NULL,
       tier TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS migration_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL,
       target INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
       started_at TEXT NOT NULL, finished_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS migration_steps (run_id INTEGER NOT NULL, unit TEXT NOT NULL,
       tenants INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
       ddl INTEGER NOT NULL DEFAULT 0, ms REAL NOT NULL DEFAULT 0, PRIMARY KEY (run_id, unit))""",
    """CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
       kind TEXT NOT NULL, tenant_id TEXT, detail TEXT NOT NULL)""",
]


class Registry:
    def __init__(self, data_dir: Path):
        self.conn = connect(str(Path(data_dir) / "control.db"))
        self.lock = threading.RLock()
        for stmt in DDL:
            self.conn.execute(stmt)

    def q(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def x(self, sql: str, params: tuple | list = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    # tenants
    def add(self, tid: str, name: str, pattern: str, tier: str, status: str = "active") -> None:
        ts = now()
        self.x("INSERT INTO tenants VALUES (?,?,?,?,?,?,?)", (tid, name, pattern, tier, status, ts, ts))

    def get(self, tid: str) -> dict | None:
        rows = self.q("SELECT * FROM tenants WHERE id = ?", (tid,))
        return rows[0] if rows else None

    def all(self, pattern: str | None = None, include_deleted: bool = False) -> list[dict]:
        sql = "SELECT * FROM tenants WHERE 1=1"
        params: list = []
        if pattern:
            sql += " AND pattern = ?"
            params.append(pattern)
        if not include_deleted:
            sql += " AND status != 'deleted'"
        return self.q(sql + " ORDER BY id", params)

    def set(self, tid: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.x(f"UPDATE tenants SET {sets}, updated_at = ? WHERE id = ?", [*fields.values(), now(), tid])

    def remove(self, tid: str) -> None:
        self.x("DELETE FROM tenants WHERE id = ?", (tid,))

    # events
    def event(self, kind: str, tenant_id: str | None, /, **detail) -> None:
        self.x(
            "INSERT INTO events (ts, kind, tenant_id, detail) VALUES (?,?,?,?)",
            (now(), kind, tenant_id, json.dumps(detail, default=str)),
        )

    def events(self, limit: int = 50) -> list[dict]:
        rows = self.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return rows

    def close(self) -> None:
        self.conn.close()
