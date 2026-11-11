"""JSON logs with tenant id, and Prometheus text metrics per tenant, with no client library."""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import defaultdict
from datetime import UTC, datetime

BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


def get_logger() -> logging.Logger:
    logger = logging.getLogger("tenancylab")
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(msg: str, **fields) -> None:
    get_logger().info(msg, extra={"fields": fields})


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: dict[tuple[str, str, int], int] = defaultdict(int)  # (tenant, route, status)
        self.hist: dict[str, list[int]] = defaultdict(lambda: [0] * len(BUCKETS))
        self.sum: dict[str, float] = defaultdict(float)
        self.count: dict[str, int] = defaultdict(int)
        self.limited: dict[tuple[str, str], int] = defaultdict(int)

    def observe(self, tenant: str, route: str, status: int, seconds: float) -> None:
        with self.lock:
            self.requests[(tenant, route, status)] += 1
            h = self.hist[tenant]
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    h[i] += 1
            self.sum[tenant] += seconds
            self.count[tenant] += 1

    def rate_limited(self, tenant: str, reason: str) -> None:
        with self.lock:
            self.limited[(tenant, reason)] += 1

    def per_tenant(self) -> dict[str, dict]:
        with self.lock:
            out: dict[str, dict] = {}
            for (t, _r, s), n in self.requests.items():
                d = out.setdefault(t, {"requests": 0, "errors": 0})
                d["requests"] += n
                d["errors"] += n if s >= 400 else 0
            for t, c in self.count.items():
                out[t]["mean_ms"] = round(self.sum[t] / c * 1000, 2)
            return out

    def render(self, gauges: dict[str, float] | None = None) -> str:
        lines = [
            "# HELP tenancylab_requests_total Requests by tenant, route and status.",
            "# TYPE tenancylab_requests_total counter",
        ]
        with self.lock:
            for (t, r, s), n in sorted(self.requests.items()):
                lines.append(f'tenancylab_requests_total{{tenant="{_esc(t)}",route="{_esc(r)}",status="{s}"}} {n}')
            lines += [
                "# HELP tenancylab_request_seconds Request latency by tenant.",
                "# TYPE tenancylab_request_seconds histogram",
            ]
            for t in sorted(self.hist):
                for b, n in zip(BUCKETS, self.hist[t], strict=True):
                    lines.append(f'tenancylab_request_seconds_bucket{{tenant="{_esc(t)}",le="{b}"}} {n}')
                lines.append(f'tenancylab_request_seconds_bucket{{tenant="{_esc(t)}",le="+Inf"}} {self.count[t]}')
                lines.append(f'tenancylab_request_seconds_sum{{tenant="{_esc(t)}"}} {self.sum[t]:.6f}')
                lines.append(f'tenancylab_request_seconds_count{{tenant="{_esc(t)}"}} {self.count[t]}')
            lines += [
                "# HELP tenancylab_rate_limited_total Requests rejected by tenant limits.",
                "# TYPE tenancylab_rate_limited_total counter",
            ]
            for (t, reason), n in sorted(self.limited.items()):
                lines.append(f'tenancylab_rate_limited_total{{tenant="{_esc(t)}",reason="{_esc(reason)}"}} {n}')
        seen = set()
        for name, value in (gauges or {}).items():
            base = name.split("{")[0]
            if base not in seen:
                seen.add(base)
                lines.append(f"# TYPE {base} gauge")
            lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"
