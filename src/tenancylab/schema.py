"""Tables and versioned migrations, written once and applied to any schema prefix.

The same DDL runs against the pooled database (prefix ""), a bridge schema (prefix "t_acme__") and a
silo database (prefix ""). Every table keeps a tenant_id column in all three patterns, so a tenant's rows
are byte-identical wherever they live and a pooled to silo move can be verified with checksums.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

TABLES = ["users", "projects", "tasks", "comments", "attachments"]
# Children first, so deletes never leave orphans.
DELETE_ORDER = ["attachments", "comments", "tasks", "projects", "users"]


def _v1(s: str) -> list[str]:
    return [
        f"CREATE TABLE {s}users (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, email TEXT NOT NULL,"
        " name TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL)",
        f"CREATE TABLE {s}projects (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,"
        " description TEXT NOT NULL DEFAULT '', owner_id TEXT, created_at TEXT NOT NULL)",
        f"CREATE TABLE {s}tasks (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,"
        " title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',"
        " assignee_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        f"CREATE TABLE {s}comments (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, task_id TEXT NOT NULL,"
        " author_id TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL)",
        f"CREATE TABLE {s}attachments (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, task_id TEXT NOT NULL,"
        " filename TEXT NOT NULL, content_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,"
        " sha256 TEXT NOT NULL, created_at TEXT NOT NULL)",
        f"CREATE INDEX {s}idx_projects_tenant ON {s}projects(tenant_id)",
        f"CREATE INDEX {s}idx_tasks_project ON {s}tasks(project_id, tenant_id)",
        f"CREATE INDEX {s}idx_comments_task ON {s}comments(task_id, tenant_id)",
        f"CREATE INDEX {s}idx_attachments_task ON {s}attachments(task_id, tenant_id)",
        f"CREATE INDEX {s}idx_users_tenant ON {s}users(tenant_id)",
        # pooled needs a tenant_id index on every table; bridge and silo carry them as dead weight
        f"CREATE INDEX {s}idx_tasks_tenant ON {s}tasks(tenant_id)",
        f"CREATE INDEX {s}idx_comments_tenant ON {s}comments(tenant_id)",
        f"CREATE INDEX {s}idx_attachments_tenant ON {s}attachments(tenant_id)",
    ]


def _v2(s: str) -> list[str]:
    return [f"ALTER TABLE {s}tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 2"]


def _v3(s: str) -> list[str]:
    return [f"CREATE INDEX {s}idx_tasks_status ON {s}tasks(tenant_id, status)"]


def _v4(s: str) -> list[str]:
    return [f"ALTER TABLE {s}projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"]


MIGRATIONS: dict[int, tuple[str, Callable[[str], list[str]]]] = {
    1: ("base tables", _v1),
    2: ("tasks.priority", _v2),
    3: ("index tasks(status)", _v3),
    4: ("projects.archived", _v4),
}
LATEST = max(MIGRATIONS)


class MigrationFailed(RuntimeError):
    pass


def current_version(conn: sqlite3.Connection, prefix: str = "") -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (f"{prefix}schema_meta",)
    ).fetchone()
    if row is None:
        return 0
    return conn.execute(f"SELECT version FROM {prefix}schema_meta").fetchone()[0]


def migrate(conn: sqlite3.Connection, target: int, prefix: str = "", fail: bool = False) -> int:
    """Apply pending migrations up to target in one transaction. Returns the DDL statement count.

    With fail=True the last migration raises after its DDL ran, to show that SQLite rolls DDL back and the
    unit stays on its old version (what a failed rolling migration looks like for one tenant).
    """
    start = current_version(conn, prefix)
    if start >= target:
        return 0
    ddl = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        if start == 0:
            conn.execute(f"CREATE TABLE {prefix}schema_meta (version INTEGER NOT NULL)")
            conn.execute(f"INSERT INTO {prefix}schema_meta VALUES (0)")
        for v in range(start + 1, target + 1):
            for stmt in MIGRATIONS[v][1](prefix):
                conn.execute(stmt)
                ddl += 1
            conn.execute(f"UPDATE {prefix}schema_meta SET version = ?", (v,))
        if fail:
            raise MigrationFailed(f"simulated failure after {ddl} statements")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return ddl
