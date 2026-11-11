"""The repository interface every backend implements, written once.

A TenantRepo is bound to one tenant. Backends only differ in three hooks:
- how a connection is checked out (shared pool, shared pool plus ATTACH, or the tenant's own pool),
- how a table is named (a guarded view, a schema-qualified table, or a plain table),
- the scope clause (pooled adds tenant_id = ?, bridge and silo rely on physical separation).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .schema import DELETE_ORDER, TABLES

if TYPE_CHECKING:
    from .backends import Backend

TASK_FIELDS = {"title", "body", "status", "assignee_id", "priority"}
PROJECT_FIELDS = {"name", "description", "archived"}
STATUSES = {"open", "in_progress", "done"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def checksum_rows(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: r["id"]):
        h.update(json.dumps(r, sort_keys=True, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()


class TenantRepo:
    def __init__(self, backend: Backend, tenant_id: str):
        self.backend = backend
        self.tenant_id = tenant_id

    # hooks ---------------------------------------------------------------
    def _conn(self) -> AbstractContextManager[sqlite3.Connection]:
        return self.backend.checkout(self.tenant_id)

    def _t(self, name: str, write: bool = False) -> str:
        return self.backend.table_ref(self.tenant_id, name, write)

    def _scope(self, alias: str = "") -> tuple[str, list]:
        return self.backend.scope(self.tenant_id, alias)

    def _one(self, sql: str, params: list) -> dict | None:
        with self._conn() as c:
            row = c.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: list) -> list[dict]:
        with self._conn() as c:
            return _rows(c.execute(sql, params))

    def _insert(self, table: str, row: dict) -> dict:
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        with self._conn() as c:
            c.execute(f"INSERT INTO {self._t(table, True)} ({cols}) VALUES ({marks})", list(row.values()))
        return self._get(table, row["id"])  # type: ignore[return-value]

    def _get(self, table: str, id_: str) -> dict | None:
        w, p = self._scope()
        return self._one(f"SELECT * FROM {self._t(table)} WHERE {w} AND id = ?", [*p, id_])

    def _update(self, table: str, id_: str, fields: dict) -> dict | None:
        if not fields:
            return self._get(table, id_)
        w, p = self._scope()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE {self._t(table, True)} SET {sets} WHERE {w} AND id = ?", [*fields.values(), *p, id_]
            )
            if cur.rowcount == 0:
                return None
        return self._get(table, id_)

    # users ---------------------------------------------------------------
    def create_user(self, email: str, name: str, role: str = "member", id: str | None = None) -> dict:
        row = {"id": id or new_id("usr"), "tenant_id": self.tenant_id, "email": email, "name": name, "role": role}
        return self._insert("users", {**row, "created_at": now()})

    def list_users(self) -> list[dict]:
        w, p = self._scope()
        return self._all(f"SELECT * FROM {self._t('users')} WHERE {w} ORDER BY email", p)

    def get_user(self, id_: str) -> dict | None:
        return self._get("users", id_)

    # projects ------------------------------------------------------------
    def create_project(self, name: str, description: str = "", owner_id: str | None = None, id: str | None = None):
        row = {"id": id or new_id("prj"), "tenant_id": self.tenant_id, "name": name, "description": description}
        return self._insert("projects", {**row, "owner_id": owner_id, "created_at": now()})

    def get_project(self, id_: str) -> dict | None:
        return self._get("projects", id_)

    def list_projects(self) -> list[dict]:
        w, p = self._scope()
        return self._all(f"SELECT * FROM {self._t('projects')} WHERE {w} ORDER BY created_at, id", p)

    def update_project(self, id_: str, **fields) -> dict | None:
        return self._update("projects", id_, {k: v for k, v in fields.items() if k in PROJECT_FIELDS})

    def delete_project(self, id_: str) -> bool:
        if self.get_project(id_) is None:
            return False
        w, p = self._scope()
        sub = f"SELECT id FROM {self._t('tasks')} WHERE {w} AND project_id = ?"
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            for child in ("attachments", "comments"):
                c.execute(f"DELETE FROM {self._t(child, True)} WHERE {w} AND task_id IN ({sub})", [*p, *p, id_])
            c.execute(f"DELETE FROM {self._t('tasks', True)} WHERE {w} AND project_id = ?", [*p, id_])
            c.execute(f"DELETE FROM {self._t('projects', True)} WHERE {w} AND id = ?", [*p, id_])
            c.execute("COMMIT")
        return True

    # tasks ---------------------------------------------------------------
    def create_task(
        self,
        project_id: str,
        title: str,
        body: str = "",
        assignee_id: str | None = None,
        status: str = "open",
        priority: int | None = None,
        id: str | None = None,
    ) -> dict | None:
        if self.get_project(project_id) is None:
            return None
        ts = now()
        row = {"id": id or new_id("tsk"), "tenant_id": self.tenant_id, "project_id": project_id, "title": title}
        row |= {"body": body, "status": status, "assignee_id": assignee_id, "created_at": ts, "updated_at": ts}
        if priority is not None:
            row["priority"] = priority
        return self._insert("tasks", row)

    def get_task(self, id_: str) -> dict | None:
        return self._get("tasks", id_)

    def list_tasks(self, project_id: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
        w, p = self._scope()
        sql = f"SELECT * FROM {self._t('tasks')} WHERE {w}"
        if project_id:
            sql += " AND project_id = ?"
            p = [*p, project_id]
        if status:
            sql += " AND status = ?"
            p = [*p, status]
        return self._all(sql + " ORDER BY created_at, id LIMIT ?", [*p, limit])

    def update_task(self, id_: str, **fields) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in TASK_FIELDS}
        if fields:
            fields["updated_at"] = now()
        return self._update("tasks", id_, fields)

    # comments and attachments --------------------------------------------
    def add_comment(self, task_id: str, body: str, author_id: str | None = None, id: str | None = None):
        if self.get_task(task_id) is None:
            return None
        row = {"id": id or new_id("cmt"), "tenant_id": self.tenant_id, "task_id": task_id, "author_id": author_id}
        return self._insert("comments", {**row, "body": body, "created_at": now()})

    def list_comments(self, task_id: str) -> list[dict]:
        w, p = self._scope()
        sql = f"SELECT * FROM {self._t('comments')} WHERE {w} AND task_id = ? ORDER BY created_at, id"
        return self._all(sql, [*p, task_id])

    def add_attachment(self, task_id: str, filename: str, content_type: str, size_bytes: int, sha256: str, id=None):
        if self.get_task(task_id) is None:
            return None
        row = {"id": id or new_id("att"), "tenant_id": self.tenant_id, "task_id": task_id, "filename": filename}
        row |= {"content_type": content_type, "size_bytes": size_bytes, "sha256": sha256, "created_at": now()}
        return self._insert("attachments", row)

    def list_attachments(self, task_id: str) -> list[dict]:
        w, p = self._scope()
        sql = f"SELECT * FROM {self._t('attachments')} WHERE {w} AND task_id = ? ORDER BY created_at, id"
        return self._all(sql, [*p, task_id])

    # search and reports --------------------------------------------------
    def search(self, q: str, limit: int = 50) -> dict:
        w, p = self._scope()
        like = f"%{q}%"
        tasks = self._all(
            f"SELECT * FROM {self._t('tasks')} WHERE {w} AND (title LIKE ? OR body LIKE ?) ORDER BY id LIMIT ?",
            [*p, like, like, limit],
        )
        comments = self._all(
            f"SELECT * FROM {self._t('comments')} WHERE {w} AND body LIKE ? ORDER BY id LIMIT ?", [*p, like, limit]
        )
        return {"tasks": tasks, "comments": comments}

    def report(self) -> dict:
        """The heavy query: per project task counts, completion, comment volume, and busiest assignees."""
        wp, p = self._scope("p.")
        wt, pt = self._scope("t.")
        wc, pc = self._scope("c.")
        projects = self._all(
            f"""SELECT p.id, p.name, COUNT(DISTINCT t.id) AS tasks,
                   COUNT(DISTINCT CASE WHEN t.status = 'done' THEN t.id END) AS done,
                   COUNT(c.id) AS comments, COALESCE(SUM(length(c.body)), 0) AS comment_chars
            FROM {self._t("projects")} p
            JOIN {self._t("tasks")} t ON t.project_id = p.id AND {wt}
            LEFT JOIN {self._t("comments")} c ON c.task_id = t.id AND {wc}
            WHERE {wp} GROUP BY p.id ORDER BY tasks DESC, p.id""",
            [*pt, *pc, *p],
        )
        assignees = self._all(
            f"""SELECT t.assignee_id, COUNT(*) AS open_tasks FROM {self._t("tasks")} t
            WHERE {wt} AND t.status != 'done' GROUP BY t.assignee_id ORDER BY open_tasks DESC LIMIT 10""",
            pt,
        )
        return {"tenant_id": self.tenant_id, "projects": projects, "top_assignees": assignees}

    # whole-tenant operations ---------------------------------------------
    def counts(self) -> dict[str, int]:
        w, p = self._scope()
        with self._conn() as c:
            return {t: c.execute(f"SELECT COUNT(*) FROM {self._t(t)} WHERE {w}", p).fetchone()[0] for t in TABLES}

    def export(self) -> dict[str, list[dict]]:
        w, p = self._scope()
        with self._conn() as c:
            return {t: _rows(c.execute(f"SELECT * FROM {self._t(t)} WHERE {w} ORDER BY id", p)) for t in TABLES}

    def checksums(self) -> dict[str, str]:
        data = self.export()
        sums = {t: checksum_rows(rows) for t, rows in data.items()}
        sums["all"] = hashlib.sha256("".join(sums[t] for t in TABLES).encode()).hexdigest()
        return sums

    def import_rows(self, data: dict[str, list[dict]]) -> int:
        n = 0
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                for t in TABLES:
                    for row in data.get(t, []):
                        if row.get("tenant_id") != self.tenant_id:
                            raise ValueError(f"row {row.get('id')} belongs to another tenant")
                        cols = ",".join(row)
                        marks = ",".join("?" for _ in row)
                        c.execute(f"INSERT INTO {self._t(t, True)} ({cols}) VALUES ({marks})", list(row.values()))
                        n += 1
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return n

    def delete_all(self) -> int:
        w, p = self._scope()
        n = 0
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            for t in DELETE_ORDER:
                n += c.execute(f"DELETE FROM {self._t(t, True)} WHERE {w}", p).rowcount
            c.execute("COMMIT")
        return n


class PlantedBugRepo(TenantRepo):
    """Pooled only, behind TENANCYLAB_PLANTED_BUG=1: a hand-written 'fast path' search that queries the base
    table directly and forgets the tenant filter. It bypasses both the query layer and the guarded view."""

    def search(self, q: str, limit: int = 50) -> dict:
        like = f"%{q}%"
        tasks = self._all(
            "SELECT * FROM main.tasks WHERE title LIKE ? OR body LIKE ? ORDER BY id LIMIT ?", [like, like, limit]
        )
        result = super().search(q, limit)
        return {"tasks": tasks, "comments": result["comments"]}


def as_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)
