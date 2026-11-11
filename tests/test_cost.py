import pytest

from tenancylab import cost
from tenancylab.cost import Assumptions, monthly


def test_instance_counts():
    assert cost.instances("pooled", 10) == 1
    assert cost.instances("pooled", 2_001) == 2
    assert cost.instances("bridge", 1_000) == 2
    assert cost.instances("silo", 37) == 37


def test_pooled_total_by_hand():
    a = Assumptions()
    r = monthly("pooled", 100, a)
    compute = 700
    storage = 100 * 2 * 0.115
    backups = 100 * 2 * 1.0 * 0.095
    ops = (20 + 0.5 * 1 + 4 * 0.5 * 1) * 120
    assert r["total_usd"] == pytest.approx(compute + storage + backups + ops, abs=0.01)
    assert r["per_tenant_usd"] == pytest.approx(r["total_usd"] / 100, abs=0.01)


def test_silo_storage_uses_minimum_volume():
    r = monthly("silo", 10)
    assert r["storage_usd"] == pytest.approx(10 * 20 * 0.115)
    assert r["connections"] == 10 * 6 * 5


def test_silo_needs_proxies_at_scale():
    assert monthly("silo", 100)["proxies"] == 0
    r = monthly("silo", 10_000)
    assert r["connections"] == 300_000 and r["proxies"] == 60


def test_per_tenant_cost_falls_for_pooled_and_stays_high_for_silo():
    rows = {(r["pattern"], r["tenants"]): r["per_tenant_usd"] for r in cost.table()}
    assert rows[("pooled", 10_000)] < rows[("pooled", 10)] / 100
    assert rows[("silo", 10_000)] > 50 * rows[("pooled", 10_000)]
    assert rows[("pooled", 1_000)] < rows[("bridge", 1_000)] < rows[("silo", 1_000)]


def test_model_lists_assumptions():
    m = cost.model()
    assert m["assumptions"]["silo_instance_usd"] == 60.0
    assert len(m["rows"]) == 12
