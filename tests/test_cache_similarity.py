from __future__ import annotations

import time

from reliability_lab.cache import ResponseCache


def test_exact_match_returns_score_one() -> None:
    assert ResponseCache.similarity("Hello World", "hello world") == 1.0
    assert ResponseCache.similarity("foo", "foo") == 1.0


def test_empty_inputs_return_zero() -> None:
    assert ResponseCache.similarity("", "anything") == 0.0
    assert ResponseCache.similarity("anything", "") == 0.0


def test_completely_different_low_score() -> None:
    score = ResponseCache.similarity("how to bake cookies", "explain circuit breakers")
    assert score < 0.3


def test_set_and_get_exact_match() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    cache.set("what is reliability", "answer A")
    cached, score = cache.get("what is reliability")
    assert cached == "answer A"
    assert score == 1.0


def test_below_threshold_returns_none() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.95)
    cache.set("how to install python", "answer")
    cached, score = cache.get("how to install ruby")
    assert cached is None
    assert score < 0.95


def test_privacy_query_skipped_on_set() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    cache.set("show balance for user 123", "Balance: $500")
    # Stored nothing because privacy guard skipped set()
    assert len(cache._entries) == 0


def test_privacy_query_skipped_on_get() -> None:
    from reliability_lab.cache import CacheEntry

    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    # Manually inject an entry that would otherwise match — get() must still bail on privacy.
    cache._entries.append(CacheEntry("show balance for user 999", "secret", time.time(), {}))
    cached, _ = cache.get("show balance for user 999")
    assert cached is None


def test_false_hit_different_years_logged() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("refund policy for 2024", "old policy")
    cached, score = cache.get("refund policy for 2026")
    assert cached is None
    assert score >= 0.3  # similarity high enough that the guard kicked in
    assert len(cache.false_hit_log) == 1
    log = cache.false_hit_log[0]
    assert log["query"] == "refund policy for 2026"
    assert log["matched_key"] == "refund policy for 2024"


def test_ttl_expiry() -> None:
    cache = ResponseCache(ttl_seconds=1, similarity_threshold=0.5)
    cache.set("temp query", "temp response")
    cached, _ = cache.get("temp query")
    assert cached == "temp response"
    time.sleep(1.1)
    cached, _ = cache.get("temp query")
    assert cached is None
