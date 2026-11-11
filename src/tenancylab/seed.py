"""Deterministic synthetic project data. All names are made up."""

from __future__ import annotations

import random

from .repo import now

FIRST = ["Ana", "Ben", "Chen", "Dara", "Eli", "Fay", "Gus", "Hana", "Ivo", "Jun", "Kai", "Lena", "Milo", "Nia"]
LAST = ["Abara", "Brook", "Castell", "Dorn", "Ekwe", "Frost", "Galen", "Hollis", "Ishida", "Juarez", "Kemp"]
NOUNS = ["billing", "onboarding", "search", "invoices", "mobile app", "reports", "SSO", "exports", "audit log"]
VERBS = ["Fix", "Add", "Refactor", "Document", "Test", "Speed up", "Review", "Remove", "Migrate"]
WORDS = "the a customer page job queue retry timeout cache index column flag button email draft owner".split()
STATUSES = ["open", "open", "in_progress", "done", "done"]
FILES = [("spec.pdf", "application/pdf"), ("screen.png", "image/png"), ("notes.txt", "text/plain")]


def _id(rng: random.Random, prefix: str) -> str:
    return f"{prefix}_{rng.getrandbits(48):012x}"


def generate(tenant_id: str, projects: int, tasks_per_project: int, seed: int = 7, users: int = 8) -> dict:
    """Rows for one tenant, ready for TenantRepo.import_rows. About 1.5 comments and 0.2 attachments per task."""
    rng = random.Random(f"{seed}:{tenant_id}")
    ts = now()
    out: dict[str, list[dict]] = {"users": [], "projects": [], "tasks": [], "comments": [], "attachments": []}
    for _ in range(users):
        f, last = rng.choice(FIRST), rng.choice(LAST)
        out["users"].append(
            {
                "id": _id(rng, "usr"),
                "tenant_id": tenant_id,
                "email": f"{f}.{last}@{tenant_id}.test".lower(),
                "name": f"{f} {last}",
                "role": rng.choice(["member", "member", "admin"]),
                "created_at": ts,
            }
        )
    uids = [u["id"] for u in out["users"]]
    for p in range(projects):
        pid = _id(rng, "prj")
        out["projects"].append(
            {
                "id": pid,
                "tenant_id": tenant_id,
                "name": f"{rng.choice(NOUNS).title()} {p + 1}",
                "description": " ".join(rng.choices(WORDS, k=8)),
                "owner_id": rng.choice(uids),
                "created_at": ts,
            }
        )
        for _ in range(tasks_per_project):
            tid = _id(rng, "tsk")
            out["tasks"].append(
                {
                    "id": tid,
                    "tenant_id": tenant_id,
                    "project_id": pid,
                    "title": f"{rng.choice(VERBS)} {rng.choice(NOUNS)}",
                    "body": " ".join(rng.choices(WORDS, k=12)),
                    "status": rng.choice(STATUSES),
                    "assignee_id": rng.choice(uids),
                    "created_at": ts,
                    "updated_at": ts,
                }
            )
            for _ in range(rng.choice([0, 1, 1, 2, 3])):
                out["comments"].append(
                    {
                        "id": _id(rng, "cmt"),
                        "tenant_id": tenant_id,
                        "task_id": tid,
                        "author_id": rng.choice(uids),
                        "body": " ".join(rng.choices(WORDS, k=10)),
                        "created_at": ts,
                    }
                )
            if rng.random() < 0.2:
                name, ctype = rng.choice(FILES)
                out["attachments"].append(
                    {
                        "id": _id(rng, "att"),
                        "tenant_id": tenant_id,
                        "task_id": tid,
                        "filename": name,
                        "content_type": ctype,
                        "size_bytes": rng.randint(2_000, 900_000),
                        "sha256": f"{rng.getrandbits(256):064x}",
                        "created_at": ts,
                    }
                )
    return out
