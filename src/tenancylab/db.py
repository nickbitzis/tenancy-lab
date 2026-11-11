"""SQLite connections and a small bounded connection pool that records how long callers wait."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class PoolTimeout(RuntimeError):
    pass


class Pool:
    """A fixed-size pool. Connections are created lazily, up to size.

    In the pooled and bridge patterns one pool stands in for one database server's connection limit, so a
    tenant that holds every connection makes all other tenants wait. In the silo pattern each tenant has
    its own pool.
    """

    def __init__(self, factory: Callable[[], sqlite3.Connection], size: int, name: str = "pool"):
        self.factory = factory
        self.size = size
        self.name = name
        self._idle: list[sqlite3.Connection] = []
        self._all: list[sqlite3.Connection] = []
        self._waiters: deque[list] = deque()  # FIFO hand-off, like PgBouncer: no barging by the releaser
        self._lock = threading.Lock()
        self.wait_seconds = 0.0
        self.checkouts = 0

    def _get(self, timeout: float) -> sqlite3.Connection:
        with self._lock:
            if self._idle:
                return self._idle.pop()
            if len(self._all) < self.size:
                conn = self.factory()
                self._all.append(conn)
                return conn
            waiter = [threading.Event(), None]
            self._waiters.append(waiter)
        if not waiter[0].wait(timeout):
            with self._lock:
                if waiter[1] is None:
                    self._waiters.remove(waiter)
                    raise PoolTimeout(f"{self.name}: no connection in {timeout}s")
        return waiter[1]

    def _put(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            if self._waiters:
                waiter = self._waiters.popleft()
                waiter[1] = conn
                waiter[0].set()
            else:
                self._idle.append(conn)

    @contextmanager
    def connection(self, timeout: float = 30.0) -> Iterator[sqlite3.Connection]:
        t0 = time.perf_counter()
        conn = self._get(timeout)
        with self._lock:
            self.wait_seconds += time.perf_counter() - t0
            self.checkouts += 1
        try:
            yield conn
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            self._put(conn)

    @contextmanager
    def all_connections(self) -> Iterator[list[sqlite3.Connection]]:
        """Check out every connection that exists (used to detach a dropped bridge schema everywhere)."""
        with self._lock:
            n = len(self._all)
        held = [self._get(30) for _ in range(n)]
        try:
            yield held
        finally:
            for c in held:
                self._put(c)

    def close(self) -> None:
        with self._lock:
            for c in self._all:
                c.close()
            self._all.clear()
            self._idle.clear()
