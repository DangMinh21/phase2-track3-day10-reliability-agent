from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from pydantic import BaseModel, Field


class RunMetrics(BaseModel):
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    fallback_successes: int = 0
    static_fallbacks: int = 0
    cache_hits: int = 0
    circuit_open_count: int = 0
    recovery_time_ms: float | None = None
    estimated_cost: float = 0.0
    estimated_cost_saved: float = 0.0
    latencies_ms: list[float] = Field(default_factory=list)
    scenarios: dict[str, str] = Field(default_factory=dict)
    per_scenario: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cache_comparison: dict[str, Any] | None = None

    @property
    def availability(self) -> float:
        return self.successful_requests / self.total_requests if self.total_requests else 0.0

    @property
    def error_rate(self) -> float:
        return self.failed_requests / self.total_requests if self.total_requests else 0.0

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / self.total_requests if self.total_requests else 0.0

    @property
    def fallback_success_rate(self) -> float:
        denom = self.fallback_successes + self.static_fallbacks
        return self.fallback_successes / denom if denom else 0.0

    def percentile(self, q: float) -> float:
        return percentile(self.latencies_ms, q)

    def to_report_dict(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "total_requests": self.total_requests,
            "availability": round(self.availability, 4),
            "error_rate": round(self.error_rate, 4),
            "latency_p50_ms": round(self.percentile(50), 2),
            "latency_p95_ms": round(self.percentile(95), 2),
            "latency_p99_ms": round(self.percentile(99), 2),
            "fallback_success_rate": round(self.fallback_success_rate, 4),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "circuit_open_count": self.circuit_open_count,
            "recovery_time_ms": self.recovery_time_ms,
            "estimated_cost": round(self.estimated_cost, 6),
            "estimated_cost_saved": round(self.estimated_cost_saved, 6),
            "scenarios": self.scenarios,
        }
        if self.per_scenario:
            report["per_scenario"] = self.per_scenario
        if self.cache_comparison is not None:
            report["cache_comparison"] = self.cache_comparison
        return report

    def slo_check(self, slo: dict[str, float]) -> dict[str, dict[str, Any]]:
        """Compare metrics against SLO targets.

        slo keys: availability, latency_p95_ms, fallback_success_rate,
        cache_hit_rate, recovery_time_ms.
        Returns a dict per SLI with target, actual, met (bool).
        """
        actual_by_key = {
            "availability": self.availability,
            "latency_p95_ms": self.percentile(95),
            "fallback_success_rate": self.fallback_success_rate,
            "cache_hit_rate": self.cache_hit_rate,
            "recovery_time_ms": self.recovery_time_ms,
        }
        result: dict[str, dict[str, Any]] = {}
        for key, target in slo.items():
            actual = actual_by_key.get(key)
            if actual is None:
                result[key] = {"target": target, "actual": None, "met": False}
                continue
            # latency / recovery_time are "lower is better"
            lower_is_better = key in {"latency_p95_ms", "recovery_time_ms"}
            met = (actual <= target) if lower_is_better else (actual >= target)
            result[key] = {
                "target": target,
                "actual": round(actual, 4),
                "met": bool(met),
            }
        return result

    def write_json(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_report_dict(), indent=2, ensure_ascii=False))


def percentile(values: Iterable[float], q: float) -> float:
    values_sorted = sorted(values)
    if not values_sorted:
        return 0.0
    if q == 50:
        return float(median(values_sorted))
    k = (len(values_sorted) - 1) * q / 100
    lower = int(k)
    upper = min(lower + 1, len(values_sorted) - 1)
    weight = k - lower
    return values_sorted[lower] * (1 - weight) + values_sorted[upper] * weight
