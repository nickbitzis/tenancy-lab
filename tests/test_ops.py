import gzip
import json

import pytest

from tenancylab.backends import UnknownTenant
from tenancylab.platform import MoveFailed


def test_onboard_rejects_bad_ids_and_duplicates(plat):
    plat.onboard("acme", "Acme")
    with pytest.raises(ValueError):
        plat.onboard("acme", "Acme again")
    with pytest.raises(ValueError):
        plat.onboard("Bad_ID", "x")


def test_offboard_exports_and_verifies_delete(plat, pattern):
    plat.onboard("acme", "Acme", pattern)
    plat.onboard("keep", "Keep", pattern)
    plat.seed("acme", 2, 10)
    plat.seed("keep", 1, 5)
    keep_sums = plat.repo("keep").checksums()
    expected = plat.repo("acme").checksums()["all"]
    r = plat.offboard("acme")
    assert r["verified"] and r["rows_remaining"] == 0 and not r["storage_exists"]
    with gzip.open(r["export_path"], "rt") as f:
        export = json.load(f)
    assert export["checksums"]["all"] == expected
    assert export["counts"]["tasks"] == 20
    with pytest.raises(UnknownTenant):
        plat.repo("acme")
    assert plat.repo("keep").checksums() == keep_sums  # the neighbor is untouched


def test_backup_and_restore_single_tenant(plat, pattern):
    plat.onboard("acme", "Acme", pattern)
    plat.onboard("keep", "Keep", pattern)
    plat.seed("acme", 2, 6)
    plat.seed("keep", 1, 4)
    b = plat.backup("acme")
    repo = plat.repo("acme")
    repo.delete_project(repo.list_projects()[0]["id"])  # an accident after the backup
    keep_sums = plat.repo("keep").checksums()
    r = plat.restore("acme", b["path"])
    assert r["verified"] and r["checksum"] == b["checksum"]
    assert plat.repo("keep").checksums() == keep_sums


def test_restore_refuses_other_tenants_backup(plat):
    plat.onboard("acme", "Acme")
    plat.onboard("bolt", "Bolt")
    b = plat.backup("acme")
    with pytest.raises(ValueError):
        plat.restore("bolt", b["path"])


def test_move_pooled_to_silo_with_checksums(plat):
    plat.onboard("acme", "Acme", "pooled")
    plat.onboard("keep", "Keep", "pooled")
    plat.seed("acme", 3, 20)
    plat.seed("keep", 1, 5)
    before = plat.repo("acme").checksums()
    r = plat.move("acme", "silo")
    assert r["moved"] and r["verified"] and r["source_rows_remaining"] == 0
    assert r["source_checksums"] == r["target_checksums"] == before
    t = plat.tenant("acme")
    assert (t["pattern"], t["tier"], t["status"]) == ("silo", "enterprise", "active")
    assert plat.repo("acme").checksums() == before
    assert sum(plat.backends["pooled"].repo("acme").counts().values()) == 0
    assert plat.repo("keep").counts()["tasks"] == 5


def test_move_aborts_on_checksum_mismatch(plat):
    plat.onboard("acme", "Acme", "pooled")
    plat.seed("acme", 2, 10)
    before = plat.repo("acme").checksums()

    def corrupt(data):
        data["tasks"][0]["title"] = "flipped bit"

    with pytest.raises(MoveFailed) as exc:
        plat.move("acme", "silo", tamper=corrupt)
    assert exc.value.report["mismatched"] == ["tasks", "all"]
    t = plat.tenant("acme")
    assert (t["pattern"], t["status"]) == ("pooled", "active")
    assert plat.repo("acme").checksums() == before  # source untouched
    assert not plat.backends["silo"].has_storage("acme")  # partial copy cleaned up


def test_writes_blocked_while_moving(plat):
    from tenancylab.platform import TenantUnavailable

    plat.onboard("acme", "Acme")
    plat.registry.set("acme", status="moving")
    with pytest.raises(TenantUnavailable):
        plat.repo("acme")


def test_background_job_scoped_to_creator(plat):
    plat.onboard("acme", "Acme")
    plat.onboard("bolt", "Bolt")
    job = plat.jobs.submit("acme", "report", lambda: plat.repo("acme").report())
    plat.jobs.wait(job["id"])
    assert plat.jobs.get("acme", job["id"])["status"] == "done"
    assert plat.jobs.get("bolt", job["id"]) is None


def test_demo_seed(plat):
    from tenancylab.ops import DEMO, seed_demo

    seed_demo(plat)
    tenants = {t["id"]: t for t in plat.tenants()}
    assert len(tenants) == len(DEMO)
    assert tenants["harbor-bikes"]["pattern"] == "silo"
    assert {t["schema_version"] for t in tenants.values()} == {4}
    assert all(r["status"] == "done" for r in plat.migrations())
    assert plat.registry.get("tidewater-coffee")["status"] == "deleted"
