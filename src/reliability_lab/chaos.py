from __future__ import annotations

import json
import random
from pathlib import Path

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
    """Average time between circuit opening and the next CLOSED transition.

    Returns None if no breaker completed a full open→closed cycle.
    """
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


def run_scenario(
    config: LabConfig,
    queries: list[str],
    scenario: ScenarioConfig,
) -> tuple[RunMetrics, ReliabilityGateway]:
    """Run a single named chaos scenario and return its metrics + gateway."""
    gateway = build_gateway(
        config,
        scenario.provider_overrides or None,
        cache_override=scenario.cache_override,
    )
    metrics = RunMetrics()
    request_count = config.load_test.requests
    for _ in range(request_count):
        prompt = random.choice(queries)
        result = gateway.complete(prompt)
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

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics, gateway


def evaluate_scenario(name: str, metrics: RunMetrics) -> str:
    """Apply per-scenario pass/fail criteria.

    Falls back to "successful_requests > 0" for unknown scenarios.
    """
    if name == "primary_timeout_100":
        # Primary always fails: fallback must serve nearly all traffic, primary CB must open.
        return (
            "pass"
            if metrics.fallback_success_rate >= 0.9 and metrics.circuit_open_count >= 1
            else "fail"
        )
    if name == "primary_flaky_50":
        # Mixed mode: at least one primary CB cycle, both primary and fallback contribute.
        return (
            "pass"
            if metrics.circuit_open_count >= 1
            and metrics.fallback_successes > 0
            and metrics.successful_requests > 0
            else "fail"
        )
    if name == "all_healthy":
        # Both providers healthy: no CB trips, error rate near zero.
        return "pass" if metrics.circuit_open_count == 0 and metrics.error_rate < 0.05 else "fail"
    if name in {"cache_off", "cache_on"}:
        # Cache toggle scenarios just need to complete successfully.
        return "pass" if metrics.successful_requests > 0 else "fail"
    return "pass" if metrics.successful_requests > 0 else "fail"


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

    return combined
