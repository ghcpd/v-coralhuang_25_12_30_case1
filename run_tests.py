# run_tests.py
from __future__ import annotations

import random
import sys
from typing import Dict, List, Any, Tuple

import audit_log_service
import enhanced_audit_log_service
import runner

def verify_baseline_integrity():
    print("Verifying baseline integrity...")
    audit_log_service.init()
    print("Baseline integrity verified.")

def check_semantic_equivalence():
    print("Checking semantic equivalence...")
    enhanced_audit_log_service.init_enhanced()
    for i in range(100):
        audit_log_service._rng.seed(1337 + i)
        b_result = audit_log_service.handle_request()
        audit_log_service._rng.seed(1337 + i)
        e_result = enhanced_audit_log_service.handle_request_enhanced()
        b_query = b_result["query"]
        e_query = e_result["query"]
        # Ignore time fields as they may differ due to execution time
        b_query_filtered = {k: v for k, v in b_query.items() if k not in ['from_ts', 'to_ts']}
        e_query_filtered = {k: v for k, v in e_query.items() if k not in ['from_ts', 'to_ts']}
        if b_query_filtered != e_query_filtered:
            print(f"Query mismatch at {i}: {b_query_filtered} vs {e_query_filtered}")
            return False
        if b_result["events"] != e_result["events"]:
            print(f"Events mismatch at {i}")
            print(f"Baseline events count: {len(b_result['events'])}")
            print(f"Enhanced events count: {len(e_result['events'])}")
            if b_result["events"] and e_result["events"]:
                print(f"First baseline event: {b_result['events'][0]}")
                print(f"First enhanced event: {e_result['events'][0]}")
            return False
    print("Semantic equivalence verified.")
    return True

def benchmark(name: str, load_fn) -> Dict[str, float]:
    metrics, _ = runner.run(load_fn, warmup=10, total=100, concurrency=5)
    print(f"\n{name} Benchmark Results:")
    print(f"Count: {metrics['count']}")
    print(f"Concurrency: {metrics['concurrency']}")
    print(f"RPS: {metrics['rps']:.2f}")
    print(f"P50: {metrics['p50_ms']:.2f}ms")
    print(f"P90: {metrics['p90_ms']:.2f}ms")
    print(f"P95: {metrics['p95_ms']:.2f}ms")
    print(f"P99: {metrics['p99_ms']:.2f}ms")
    print(f"Avg: {metrics['avg_ms']:.2f}ms")
    return metrics

def main():
    try:
        verify_baseline_integrity()
        if not check_semantic_equivalence():
            print("Semantic equivalence check failed.")
            sys.exit(1)

        # Benchmark baseline
        def baseline_load():
            import time
            t0 = time.perf_counter()
            result = audit_log_service.handle_request()
            t1 = time.perf_counter()
            return t0, t1

        baseline_metrics = benchmark("Baseline", baseline_load)

        # Benchmark enhanced
        def enhanced_load():
            import time
            t0 = time.perf_counter()
            result = enhanced_audit_log_service.handle_request_enhanced()
            t1 = time.perf_counter()
            return t0, t1

        enhanced_metrics = benchmark("Enhanced", enhanced_load)

        # Check improvements
        rps_improved = enhanced_metrics["rps"] > baseline_metrics["rps"]
        p95_improved = enhanced_metrics["p95_ms"] < baseline_metrics["p95_ms"]
        p99_improved = enhanced_metrics["p99_ms"] < baseline_metrics["p99_ms"]

        print("\nImprovement Check:")
        print(f"RPS improved: {rps_improved}")
        print(f"P95 improved: {p95_improved}")
        print(f"P99 improved: {p99_improved}")

        if rps_improved and p95_improved and p99_improved:
            print("All checks passed.")
            sys.exit(0)
        else:
            print("Improvement check failed.")
            sys.exit(1)

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()