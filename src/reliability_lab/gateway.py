from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers.

    Route reasons emitted on GatewayResponse.route:
        "cache_hit:<score>"     — served from cache
        "primary:<provider>"    — served by first provider in chain
        "fallback:<provider>"   — served by a non-first provider in chain
        "static_fallback"       — all providers exhausted / circuits open
    """

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        start = time.perf_counter()

        if self.cache is not None:
            try:
                cached, score = self.cache.get(prompt)
            except Exception:
                cached, score = None, 0.0
            if cached is not None:
                latency_ms = (time.perf_counter() - start) * 1000
                return GatewayResponse(
                    text=cached,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=latency_ms,
                    estimated_cost=0.0,
                )

        errors: list[str] = []
        for idx, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            role = "primary" if idx == 0 else "fallback"
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
            except CircuitOpenError:
                errors.append(f"{provider.name}:circuit_open")
                continue
            except ProviderError as exc:
                errors.append(f"{provider.name}:provider_error:{exc}")
                continue

            if self.cache is not None:
                try:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                except Exception:
                    pass

            latency_ms = (time.perf_counter() - start) * 1000
            return GatewayResponse(
                text=response.text,
                route=f"{role}:{provider.name}",
                provider=provider.name,
                cache_hit=False,
                latency_ms=latency_ms,
                estimated_cost=response.estimated_cost,
            )

        latency_ms = (time.perf_counter() - start) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=latency_ms,
            estimated_cost=0.0,
            error="; ".join(errors) or None,
        )
