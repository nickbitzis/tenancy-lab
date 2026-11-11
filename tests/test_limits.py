import threading
import time

import pytest

from tenancylab.limits import ConcurrencyCap, Policy, RateLimited, TenantLimiter, TokenBucket


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_token_bucket_refills():
    clock = Clock()
    b = TokenBucket(rate=10, burst=5, clock=clock)
    assert all(b.take() == 0 for _ in range(5))
    assert b.take() == pytest.approx(0.1)
    clock.t = 0.5
    assert b.take(5) == 0


def test_report_cost_drains_bucket_faster():
    clock = Clock()
    lim = TenantLimiter({"starter": Policy(rate=10, burst=20, concurrency=4)}, clock=clock)
    for _ in range(2):
        with lim.admit("big", "starter", 10):
            pass
    with pytest.raises(RateLimited) as exc:
        with lim.admit("big", "starter", 10):
            pass
    assert exc.value.retry_after == pytest.approx(1.0)


def test_limits_are_per_tenant():
    clock = Clock()
    lim = TenantLimiter({"starter": Policy(rate=1, burst=1, concurrency=4)}, clock=clock)
    with lim.admit("a", "starter"):
        pass
    with pytest.raises(RateLimited):
        with lim.admit("a", "starter"):
            pass
    with lim.admit("b", "starter"):  # a's exhaustion does not touch b
        pass
    assert lim.rejected == {"a": 1}


def test_concurrency_cap_holds_under_threads():
    cap = ConcurrencyCap(2)

    def work():
        with cap.slot(5) as ok:
            assert ok
            time.sleep(0.05)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cap.peak == 2


def test_concurrency_cap_rejects_after_queue_timeout():
    lim = TenantLimiter({"starter": Policy(rate=1000, burst=1000, concurrency=1, queue_timeout=0.05)})
    with lim.admit("a", "starter"):
        with pytest.raises(RateLimited, match="concurrency"):
            with lim.admit("a", "starter"):
                pass
        with lim.admit("b", "starter"):  # other tenants keep their own slots
            pass


def test_disabled_limiter_admits_everything():
    lim = TenantLimiter({"starter": Policy(rate=0.001, burst=1, concurrency=1)}, enabled=False)
    for _ in range(10):
        with lim.admit("a", "starter", 100):
            pass


def test_noisy_neighbor_smoke(tmp_path):
    from tenancylab import noisy

    r = noisy.run(duration=0.4, small=3, large_projects=2, large_tasks=60, report_threads=3, data_dir=tmp_path)
    assert {(x["pattern"], x["mitigated"]) for x in r["results"]} == {
        (p, m) for p in ("pooled", "bridge", "silo") for m in (False, True)
    }
    for x in r["results"]:
        assert x["small_ops"] > 0
        assert x["small_p99_ms"] >= x["small_p50_ms"]
        if x["mitigated"]:
            assert x["reports_rejected"] > 0  # the big tenant hit its rate limit
        else:
            assert x["reports_rejected"] == 0


def test_percentile():
    from tenancylab.noisy import percentile

    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(list(range(101)), 99) == 99
    assert percentile([], 99) == 0.0
