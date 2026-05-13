from __future__ import annotations

from reliability_lab.chaos import run_scenario
from reliability_lab.config import (
    CacheConfig,
    CircuitBreakerConfig,
    LabConfig,
    LoadTestConfig,
    ProviderConfig,
    ScenarioConfig,
)


def _build_config(concurrency: int, requests: int, cache_enabled: bool = False) -> LabConfig:
    return LabConfig(
        providers=[
            ProviderConfig(name="p1", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001),
            ProviderConfig(name="p2", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001),
        ],
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=3, reset_timeout_seconds=1, success_threshold=1
        ),
        cache=CacheConfig(enabled=cache_enabled, ttl_seconds=60, similarity_threshold=0.5),
        load_test=LoadTestConfig(requests=requests, concurrency=concurrency),
    )


def test_concurrent_run_preserves_total_count() -> None:
    """Concurrent execution must not drop request counts due to race conditions."""
    config = _build_config(concurrency=10, requests=200)
    queries = ["q1", "q2", "q3"]
    metrics, _ = run_scenario(config, queries, ScenarioConfig(name="test"))
    assert metrics.total_requests == 200
    assert len(metrics.latencies_ms) == 200
    # All requests should succeed because providers are 0% fail.
    assert metrics.successful_requests == 200
    assert metrics.failed_requests == 0


def test_concurrent_cache_is_thread_safe() -> None:
    """Many threads hammering an in-memory cache should not crash or lose requests."""
    config = _build_config(concurrency=20, requests=200, cache_enabled=True)
    queries = ["q1", "q2", "q3"]
    metrics, gateway = run_scenario(config, queries, ScenarioConfig(name="cache_test"))
    assert metrics.total_requests == 200
    assert metrics.failed_requests == 0
    # After the initial fill race, most subsequent requests should hit the cache.
    assert metrics.cache_hits >= 100, f"expected substantial cache hits, got {metrics.cache_hits}"
    assert gateway.cache is not None


def test_sequential_run_still_works_when_concurrency_is_one() -> None:
    """Falling back to the sequential loop must remain functional."""
    config = _build_config(concurrency=1, requests=50)
    queries = ["q1"]
    metrics, _ = run_scenario(config, queries, ScenarioConfig(name="seq"))
    assert metrics.total_requests == 50
    assert len(metrics.latencies_ms) == 50
