"""The repository interface behaves the same on all three backends."""

import sqlite3

import pytest

from tenancylab.seed import generate


def _fill(repo):
    u = repo.create_user("ana@x.test", "Ana")
    p = repo.create_project("Roadmap", "Q3 plan", u["id"])
    t = repo.create_task(p["id"], "Fix billing", "invoice totals", u["id"])
    c = repo.add_comment(t["id"], "looking now", u["id"])
    a = repo.add_attachment(t["id"], "spec.pdf", "application/pdf", 1200, "a" * 64)
    return u, p, t, c, a


def test_crud_roundtrip(two):
    _, a, _ = two
    u, p, t, c, att = _fill(a)
    assert a.get_project(p["id"])["name"] == "Roadmap"
    assert a.get_task(t["id"])["title"] == "Fix billing"
    assert a.get_task(t["id"])["priority"] == 2  # v2 default
    assert [x["id"] for x in a.list_comments(t["id"])] == [c["id"]]
    assert [x["id"] for x in a.list_attachments(t["id"])] == [att["id"]]
    assert [x["id"] for x in a.list_users()] == [u["id"]]


def test_update_task_and_project(two):
    _, a, _ = two
    _, p, t, _, _ = _fill(a)
    assert a.update_task(t["id"], status="done", priority=1, tenant_id="bolt")["status"] == "done"
    assert a.get_task(t["id"])["tenant_id"] == "acme"  # tenant_id is not an updatable field
    assert a.update_project(p["id"], name="Renamed", archived=1)["archived"] == 1


def test_delete_project_cascades(two):
    _, a, _ = two
    _, p, t, _, _ = _fill(a)
    assert a.delete_project(p["id"])
    assert a.get_task(t["id"]) is None
    assert a.counts() == {"users": 1, "projects": 0, "tasks": 0, "comments": 0, "attachments": 0}


def test_missing_parent_returns_none(two):
    _, a, _ = two
    assert a.create_task("prj_missing", "x") is None
    assert a.add_comment("tsk_missing", "x") is None
    assert a.add_attachment("tsk_missing", "f", "t", 1, "a" * 64) is None
    assert a.delete_project("prj_missing") is False


def test_other_tenant_ids_are_invisible(two):
    _, a, b = two
    _, p, t, _, _ = _fill(a)
    assert b.get_project(p["id"]) is None
    assert b.get_task(t["id"]) is None
    assert b.update_task(t["id"], title="pwned") is None
    assert b.delete_project(p["id"]) is False
    assert a.get_task(t["id"])["title"] == "Fix billing"


def test_search_and_list_scoped(two):
    _, a, b = two
    _fill(a)
    _fill(b)
    assert len(a.search("billing")["tasks"]) == 1
    assert all(r["tenant_id"] == "acme" for r in a.list_tasks())
    assert len(a.list_projects()) == 1


def test_report_counts(two):
    plat, a, _ = two
    plat.seed("bolt", 3, 10)
    a.import_rows(generate("acme", 2, 5))
    r = a.report()
    assert r["tenant_id"] == "acme"
    assert sum(p["tasks"] for p in r["projects"]) == 10


def test_export_import_checksums(two):
    plat, a, b = two
    a.import_rows(generate("acme", 2, 8))
    data, sums = a.export(), a.checksums()
    a.delete_all()
    assert sum(a.counts().values()) == 0
    a.import_rows(data)
    assert a.checksums() == sums


def test_import_rejects_foreign_rows(two):
    _, a, _ = two
    with pytest.raises(ValueError):
        a.import_rows(generate("bolt", 1, 2))
    assert sum(a.counts().values()) == 0  # rolled back


def test_same_results_on_every_backend(tmp_path):
    """Load identical rows into all three backends and compare every read."""
    from tenancylab.platform import Platform

    plat = Platform(tmp_path)
    rows = generate("same", 3, 12)
    outs = []
    for pattern in ["pooled", "bridge", "silo"]:
        plat.backends[pattern].create_tenant("same", 4)
        repo = plat.backends[pattern].repo("same")
        repo.import_rows(rows)
        outs.append((repo.checksums(), repo.report()["projects"], repo.search("Fix")["tasks"], repo.counts()))
    assert outs[0] == outs[1] == outs[2]
    plat.close()


def test_pooled_guard_blocks_raw_cross_tenant_writes(plat):
    plat.onboard("acme", "Acme", "pooled")
    plat.onboard("bolt", "Bolt", "pooled")
    b = plat.repo("bolt")
    p = b.create_project("Bolt plan")
    backend = plat.backends["pooled"]
    with backend.checkout("acme") as conn:
        with pytest.raises(sqlite3.IntegrityError, match="tenant guard"):
            conn.execute("UPDATE main.projects SET name = 'pwned' WHERE id = ?", (p["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="tenant guard"):
            conn.execute("DELETE FROM main.projects WHERE id = ?", (p["id"],))
        # The guarded view hides bolt's rows even from a query that forgets the filter.
        assert conn.execute("SELECT COUNT(*) FROM temp.v_projects").fetchone()[0] == 0
    assert b.get_project(p["id"])["name"] == "Bolt plan"


def test_pooled_guard_blocks_writes_without_tenant_context(plat):
    with plat.backends["pooled"].pool.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO main.users VALUES ('u', 'acme', 'e', 'n', 'r', 't')")
