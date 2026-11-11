import pytest

from tenancylab.platform import Platform
from tenancylab.schema import LATEST


@pytest.fixture
def old(tmp_path):
    p = Platform(tmp_path, initial_version=1)
    yield p
    p.close()


def _fleet(plat, pattern, n=5):
    for i in range(n):
        plat.onboard(f"t{i}", f"T{i}", pattern, version=1)
        plat.seed(f"t{i}", 1, 3)


@pytest.mark.parametrize("pattern", ["bridge", "silo"])
def test_rolling_migration_resumes_after_failure(old, pattern):
    _fleet(old, pattern)
    run = old.start_migration(pattern)
    first = old.run_migration(run, fail_units={"t2"})
    assert first["status"] == "failed"
    assert (first["done"], first["failed"], first["pending"]) == (2, 1, 2)
    assert old.version("t2") == 1  # DDL rolled back for the failed tenant
    assert old.version("t1") == LATEST and old.version("t3") == 1
    final = old.run_migration(run)
    assert final["status"] == "done" and final["done"] == 5
    assert final["recovered_units"] == ["t2"]
    assert final["attempts"] == 2
    assert {old.version(f"t{i}") for i in range(5)} == {LATEST}


def test_pooled_migration_is_one_unit(old):
    _fleet(old, "pooled")
    run = old.start_migration("pooled")
    s = old.run_migration(run, fail_units={"shared"})
    assert s["status"] == "failed" and s["units"] == 1 and s["tenants"] == 5
    assert old.version("t0") == 1
    s = old.run_migration(run)
    assert s["status"] == "done" and s["ddl_statements"] == 3
    assert old.repo("t0").list_tasks()[0]["priority"] == 2


def test_runner_killed_midway_then_resumed(old):
    _fleet(old, "silo", 6)
    run = old.start_migration("silo")
    s = old.run_migration(run, max_units=4)
    assert s["status"] == "paused" and s["done"] == 4
    s = old.run_migration(run)
    assert s["status"] == "done" and s["done"] == 6
    assert s["step_attempts"] == 6  # finished units were not re-run


def test_effort_scales_with_units(old):
    _fleet(old, "bridge", 4)
    s = old.run_migration(old.start_migration("bridge"))
    assert s["units"] == 4 and s["ddl_statements"] == 4 * 3


def test_migration_is_idempotent(old):
    _fleet(old, "silo", 2)
    old.run_migration(old.start_migration("silo"))
    s = old.run_migration(old.start_migration("silo"))
    assert s["status"] == "done" and s["ddl_statements"] == 0
