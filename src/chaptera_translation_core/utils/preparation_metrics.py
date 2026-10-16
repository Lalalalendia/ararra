from __future__ import annotations
import threading
import time
from collections import Counter
_preparation_sink = None

def register_preparation_sink(callback):
    """Optional application diagnostics hook; core has no app dependency."""
    global _preparation_sink
    _preparation_sink = callback

_lock = threading.RLock()
_counts: Counter[str] = Counter()
_durations_ms: Counter[str] = Counter()
def record_preparation_event(name: str, *, duration_ms: float = 0.0, count: int = 1) -> None:
    # Accumulates low-overhead preparation metrics and mirrors them to the application profiler when available.
    key = str(name or 'unknown')
    with _lock:
        _counts[key] += max(0, int(count or 0))
        _durations_ms[key] += max(0.0, float(duration_ms or 0.0))
    callback = _preparation_sink
    if callback is not None:
        callback(key, max(0.0, float(duration_ms or 0.0)), max(0, int(count or 0)))
def timed_preparation_event(name: str):
    # Returns a tiny context manager used to measure deterministic preparation work without disk writes.
    class _Timer:
        def __enter__(self):
            # Starts one preparation timing sample.
            self.started = time.perf_counter()
            return self
        def __exit__(self, exc_type, exc, tb):
            # Records elapsed preparation time and whether the measured block completed.
            elapsed_ms = (time.perf_counter() - self.started) * 1000.0
            record_preparation_event(name, duration_ms=elapsed_ms, count=1)
            return False
    return _Timer()
def preparation_metrics_snapshot() -> dict[str, dict[str, float]]:
    # Returns an immutable-friendly copy of the current in-memory preparation counters for tests and diagnostics.
    with _lock:
        keys = sorted(set(_counts).union(_durations_ms))
        return {
            key: {
                'count': int(_counts.get(key, 0)),
                'total_ms': float(_durations_ms.get(key, 0.0)),
            }
            for key in keys
        }
def reset_preparation_metrics() -> None:
    # Clears only in-memory preparation counters without touching persisted performance history.
    with _lock:
        _counts.clear()
        _durations_ms.clear()
