import pytest

from reliability_lab.cache import ResponseCache


@pytest.mark.todo
def test_semantic_cache_should_not_false_hit_different_intent() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
    cached, _ = cache.get("Summarize refund policy for 2026 deadline")
    assert cached is None
    assert len(cache.false_hit_log) == 1
    assert cache.false_hit_log[0]["matched_key"] == "Summarize refund policy for 2024 deadline"
