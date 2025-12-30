# runner.py (BASELINE — DO NOT EDIT)
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Tuple

def _percentile(xs: List[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)

def run(load_fn: Callable[[], Tuple[float, float]], warmup: int, total: int, concurrency: int) -> Tuple[dict, List[float]]:
    latencies: List[float] = []

    for _ in range(warmup):
        load_fn()

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(load_fn) for _ in range(total)]
        for fut in as_completed(futs):
            t0, t1 = fut.result()
            latencies.append((t1 - t0) * 1000.0)
    end = time.perf_counter()

    rps = total / (end - start)
    metrics = {
        "count": total,
        "concurrency": concurrency,
        "rps": rps,
        "p50_ms": _percentile(latencies, 0.50),
        "p90_ms": _percentile(latencies, 0.90),
        "p95_ms": _percentile(latencies, 0.95),
        "p99_ms": _percentile(latencies, 0.99),
        "avg_ms": statistics.mean(latencies),
    }
    return metrics, latencies
