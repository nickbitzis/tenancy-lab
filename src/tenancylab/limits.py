"""Tenant-aware rate limits (token bucket) and per-tenant concurrency caps.

The rate limit rejects with a Retry-After. The concurrency cap queues the request in the tenant's own lane
for a short time and then rejects, so a tenant that fires many heavy reports waits behind itself instead
of holding every shared database connection.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


class RateLimited(Exception):
    def __init__(self, tenant_id: str, retry_after: float, reason: str):
        super().__init__(f"{tenant_id}: {reason}")
        self.tenant_id = tenant_id
        self.retry_after = retry_after
        self.reason = reason


@dataclass(frozen=True)
class Policy:
    rate: float  # tokens per second
    burst: float  # bucket size
    concurrency: int  # requests in flight per tenant
    queue_timeout: float = 2.0  # seconds to wait for a concurrency slot


# A request costs 1 token; a heavy report costs REPORT_COST.
REPORT_COST = 10
TIER_POLICIES = {
    "starter": Policy(rate=50, burst=100, concurrency=4),
    "growth": Policy(rate=150, burst=300, concurrency=6),
    "enterprise": Policy(rate=500, burst=1000, concurrency=16),
}


class TokenBucket:
    def __init__(self, rate: float, burst: float, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens = burst
        self.ts = clock()
        self.lock = threading.Lock()

    def take(self, cost: float = 1.0) -> float:
        """Take cost tokens. Returns 0 on success, otherwise seconds until enough tokens exist."""
        with self.lock:
            t = self.clock()
            self.tokens = min(self.burst, self.tokens + (t - self.ts) * self.rate)
            self.ts = t
            if self.tokens >= cost:
                self.tokens -= cost
                return 0.0
            return (cost - self.tokens) / self.rate


class ConcurrencyCap:
    def __init__(self, limit: int):
        self.limit = limit
        self.sem = threading.BoundedSemaphore(limit)
        self.in_flight = 0
        self.peak = 0
        self.lock = threading.Lock()

    @contextmanager
    def slot(self, timeout: float) -> Iterator[bool]:
        ok = self.sem.acquire(timeout=timeout)
        if not ok:
            yield False
            return
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            yield True
        finally:
            with self.lock:
                self.in_flight -= 1
            self.sem.release()


class TenantLimiter:
    def __init__(self, policies: dict[str, Policy] | None = None, enabled: bool = True, clock=time.monotonic):
        self.policies = policies or TIER_POLICIES
        self.enabled = enabled
        self.clock = clock
        self.buckets: dict[str, TokenBucket] = {}
        self.caps: dict[str, ConcurrencyCap] = {}
        self.rejected: dict[str, int] = {}
        self.lock = threading.Lock()

    def policy(self, tier: str) -> Policy:
        return self.policies.get(tier, self.policies["starter"])

    def _state(self, tid: str, tier: str) -> tuple[TokenBucket, ConcurrencyCap]:
        with self.lock:
            if tid not in self.buckets:
                p = self.policy(tier)
                self.buckets[tid] = TokenBucket(p.rate, p.burst, self.clock)
                self.caps[tid] = ConcurrencyCap(p.concurrency)
            return self.buckets[tid], self.caps[tid]

    def _reject(self, tid: str, retry: float, reason: str) -> RateLimited:
        with self.lock:
            self.rejected[tid] = self.rejected.get(tid, 0) + 1
        return RateLimited(tid, retry, reason)

    @contextmanager
    def admit(self, tid: str, tier: str, cost: float = 1.0) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        bucket, cap = self._state(tid, tier)
        wait = bucket.take(cost)
        if wait > 0:
            raise self._reject(tid, wait, "rate limit")
        with cap.slot(self.policy(tier).queue_timeout) as ok:
            if not ok:
                raise self._reject(tid, 1.0, "concurrency cap")
            yield

    def reset(self, tid: str) -> None:
        with self.lock:
            self.buckets.pop(tid, None)
            self.caps.pop(tid, None)
