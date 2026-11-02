"""Bounded operational metrics. Never store user IDs, text or amounts."""
from collections import defaultdict
from contextlib import contextmanager
from threading import RLock
from time import perf_counter
from functools import wraps

OPERATIONS = frozenset({"interpret", "analysis", "transcribe", "receipt", "voice",
                        "reminder", "telegram_command", "telegram_text", "telegram_audio",
                        "telegram_photo", "telegram_location", "classifier"})
BOUNDS = (0.1, 0.5, 1, 5, 15, 60, 120, float("inf"))


class Metrics:
    def __init__(self):
        self._lock = RLock()
        self._counts = defaultdict(int)
        self._durations = {}
        self._http = defaultdict(int)

    def operation(self, operation, elapsed, *, success=True):
        if operation not in OPERATIONS:
            raise ValueError("unknown operation")
        with self._lock:
            self._counts[(operation, "ok" if success else "error")] += 1
            buckets, total, count = self._durations.get(operation, ([0] * len(BOUNDS), 0.0, 0))
            elapsed = max(0.0, float(elapsed))
            for i, bound in enumerate(BOUNDS):
                if elapsed <= bound:
                    buckets[i] += 1
            self._durations[operation] = buckets, total + elapsed, count + 1

    @contextmanager
    def measure(self, operation):
        started, success = perf_counter(), False
        try:
            yield
            success = True
        finally:
            self.operation(operation, perf_counter() - started, success=success)

    def http(self, method, route, status):
        method = method if method in {"GET", "POST", "PATCH", "DELETE", "PUT", "HEAD", "OPTIONS"} else "OTHER"
        # Callers provide the matched route template, never the actual URL.
        route = route if isinstance(route, str) and route.startswith("/") and len(route) <= 160 else "unmatched"
        with self._lock:
            self._http[(method, route, str(int(status)))] += 1

    def render(self):
        def labels(**values):
            def escape(value):
                return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            return "{" + ",".join(f'{key}="{escape(value)}"' for key, value in values.items()) + "}"
        lines = ["# HELP tge_operations_total Completed operations by result.",
                 "# TYPE tge_operations_total counter"]
        with self._lock:
            for (operation, status), value in sorted(self._counts.items()):
                lines.append(f"tge_operations_total{labels(operation=operation, status=status)} {value}")
            lines += ["# HELP tge_operation_duration_seconds Operation latency in seconds.",
                      "# TYPE tge_operation_duration_seconds histogram"]
            for operation, (buckets, total, count) in sorted(self._durations.items()):
                for bound, value in zip(BOUNDS, buckets):
                    label = "+Inf" if bound == float("inf") else str(bound)
                    lines.append(f"tge_operation_duration_seconds_bucket{labels(operation=operation, le=label)} {value}")
                lines.append(f"tge_operation_duration_seconds_sum{labels(operation=operation)} {total:.9f}")
                lines.append(f"tge_operation_duration_seconds_count{labels(operation=operation)} {count}")
            lines += ["# HELP tge_http_requests_total HTTP requests by route template and status.",
                      "# TYPE tge_http_requests_total counter"]
            for (method, route, status), value in sorted(self._http.items()):
                lines.append(f"tge_http_requests_total{labels(method=method, route=route, status=status)} {value}")
        return "\n".join(lines) + "\n"


metrics = Metrics()


def instrument(operation):
    def decorate(function):
        @wraps(function)
        async def measured(*args, **kwargs):
            with metrics.measure(operation):
                return await function(*args, **kwargs)
        return measured
    return decorate
