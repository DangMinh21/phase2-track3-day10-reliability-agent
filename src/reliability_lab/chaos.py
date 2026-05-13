from __future__ import annotations

import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
    cache_override: bool | None = None,
) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache_enabled = config.cache.enabled if cache_override is None else cache_override
    cache: ResponseCache | SharedRedisCache | None = None
    if cache_enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def _record_result(metrics: RunMetrics, result: Any, lock: threading.Lock) -> None:
    """Thread-safe metrics update for a single gateway response."""
    with lock:
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        if result.route.startswith("fallback"):
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result.latency_ms:
            metrics.latencies_ms.append(result.latency_ms)


def run_scenario(
    config: LabConfig,
    queries: list[str],
    scenario: ScenarioConfig,
) -> tuple[RunMetrics, ReliabilityGateway]:
    """Run a single named chaos scenario sequentially or concurrently."""
    gateway = build_gateway(
        config,
        scenario.provider_overrides or None,
        cache_override=scenario.cache_override,
    )
    metrics = RunMetrics()
    metrics_lock = threading.Lock()
    request_count = config.load_test.requests
    concurrency = max(1, config.load_test.concurrency)

    def execute(_: int) -> None:
        prompt = random.choice(queries)
        result = gateway.complete(prompt)
        _record_result(metrics, result, metrics_lock)

    if concurrency == 1:
        for i in range(request_count):
            execute(i)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(execute, range(request_count)))

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for t in breaker.transition_log
        if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics, gateway


def evaluate_scenario(name: str, metrics: RunMetrics) -> str:
    if name == "primary_timeout_100":
        return (
            "pass"
            if metrics.fallback_success_rate >= 0.9 and metrics.circuit_open_count >= 1
            else "fail"
        )
    if name == "primary_flaky_50":
        return (
            "pass"
            if metrics.circuit_open_count >= 1
            and metrics.fallback_successes > 0
            and metrics.successful_requests > 0
            else "fail"
        )
    if name == "all_healthy":
        return "pass" if metrics.circuit_open_count == 0 and metrics.error_rate < 0.05 else "fail"
    if name in {"cache_off", "cache_on"}:
        return "pass" if metrics.successful_requests > 0 else "fail"
    return "pass" if metrics.successful_requests > 0 else "fail"


def _build_cache_comparison(per_scenario: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Build a cache vs no-cache comparison block when both scenarios are present."""
    if "cache_off" not in per_scenario or "cache_on" not in per_scenario:
        return None
    off = per_scenario["cache_off"]
    on = per_scenario["cache_on"]
    keys = ("latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate", "availability")
    without = {k: off[k] for k in keys}
    with_cache = {k: on[k] for k in keys}
    delta = {
        "latency_p50_ms": round(on["latency_p50_ms"] - off["latency_p50_ms"], 2),
        "latency_p95_ms": round(on["latency_p95_ms"] - off["latency_p95_ms"], 2),
        "estimated_cost": round(on["estimated_cost"] - off["estimated_cost"], 6),
        "cache_hit_rate": round(on["cache_hit_rate"] - off["cache_hit_rate"], 4),
    }
    return {"without_cache": without, "with_cache": with_cache, "delta": delta}


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all configured scenarios and aggregate into a single RunMetrics."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics, _ = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": evaluate_scenario("default", metrics)}
        metrics.per_scenario = {"default": metrics.to_report_dict()}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result, _ = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = evaluate_scenario(scenario.name, result)
        combined.per_scenario[scenario.name] = result.to_report_dict()

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    combined.cache_comparison = _build_cache_comparison(combined.per_scenario)
    return combined
