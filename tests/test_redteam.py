"""Cross-tenant leak attempts on every endpoint, for every backend. This file is the CI isolation gate."""

import pytest

from tenancylab import redteam

pytestmark = pytest.mark.redteam
RUNS: dict = {}


def _run(pattern: str, planted_bug: bool = False) -> dict:
    key = (pattern, planted_bug)
    if key not in RUNS:
        RUNS[key] = redteam.run_backend(pattern, planted_bug)
    return RUNS[key]


@pytest.mark.parametrize("attack", redteam.ATTACK_NAMES)
@pytest.mark.parametrize("pattern", ["pooled", "bridge", "silo"])
def test_no_leak(pattern, attack):
    row = next(r for r in _run(pattern)["results"] if r["attack"] == attack)
    assert not row["leaked"], row["detail"]


def test_every_attack_ran_on_every_backend():
    for pattern in ["pooled", "bridge", "silo"]:
        assert [r["attack"] for r in _run(pattern)["results"]] == redteam.ATTACK_NAMES


def test_planted_bug_is_caught():
    run = _run("pooled", planted_bug=True)
    assert run["leaks"] > 0
    assert "search_canary" in run["leaked_attacks"]
    # Only the search path is broken; every other attack still holds.
    assert set(run["leaked_attacks"]) <= {"search_canary", "search_wildcard"}
