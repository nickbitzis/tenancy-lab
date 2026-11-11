import logging

import pytest

from tenancylab.platform import Platform

logging.getLogger("tenancylab").disabled = True

PATTERNS = ["pooled", "bridge", "silo"]


@pytest.fixture
def plat(tmp_path):
    p = Platform(tmp_path / "data")
    yield p
    p.close()


@pytest.fixture(params=PATTERNS)
def pattern(request):
    return request.param


@pytest.fixture
def two(plat, pattern):
    """Two tenants on the same pattern, returning (platform, repo_a, repo_b)."""
    plat.onboard("acme", "Acme", pattern)
    plat.onboard("bolt", "Bolt", pattern)
    return plat, plat.repo("acme"), plat.repo("bolt")
