from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory cache with hybrid similarity and false-hit guardrails.

    Similarity scoring (see `similarity` classmethod):
        - Exact match (lowercase + strip) → 1.0
        - Otherwise: 0.5 * Jaccard(tokens) + 0.5 * Jaccard(char trigrams)

    Safety:
        - `_is_uncacheable` skips privacy-sensitive queries on both get() and set().
        - `_looks_like_false_hit` rejects high-similarity matches with different
          4-digit numbers (years, IDs) and records them in `false_hit_log`.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []
        self._lock = threading.RLock()

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        with self._lock:
            now = time.time()
            self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

            best_entry: CacheEntry | None = None
            best_score = 0.0
            for entry in self._entries:
                score = self.similarity(query, entry.key)
                if score > best_score:
                    best_score = score
                    best_entry = entry

            if best_entry is None or best_score < self.similarity_threshold:
                return None, best_score

            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "matched_key": best_entry.key,
                        "score": best_score,
                        "ts": now,
                    }
                )
                return None, best_score

            return best_entry.value, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        with self._lock:
            self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Hybrid similarity: exact-match shortcut, then average of token and trigram Jaccard."""
        al = a.lower().strip()
        bl = b.lower().strip()
        if not al or not bl:
            return 0.0
        if al == bl:
            return 1.0

        tokens_a = set(al.split())
        tokens_b = set(bl.split())
        token_score = (
            len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if tokens_a and tokens_b else 0.0
        )

        trigrams_a = {al[i : i + 3] for i in range(len(al) - 2)} or {al}
        trigrams_b = {bl[i : i + 3] for i in range(len(bl) - 2)} or {bl}
        trigram_score = len(trigrams_a & trigrams_b) / len(trigrams_a | trigrams_b)

        return 0.5 * token_score + 0.5 * trigram_score


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    TODO(student): Implement the get() and set() methods using Redis commands
    so that cache state is shared across multiple gateway instances.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis with exact-match fast path then similarity scan.

        Graceful degradation: any Redis error returns (None, 0.0) so callers treat the
        lookup as a cache miss instead of crashing.
        """
        if _is_uncacheable(query):
            return None, 0.0

        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            exact = self._redis.hget(key, "response")
            if exact is not None:
                return exact, 1.0

            best_response: str | None = None
            best_query: str | None = None
            best_score = 0.0
            for k in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(k, "query")
                if cached_query is None:
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_query = cached_query
                    best_response = self._redis.hget(k, "response")

            if best_response is None or best_score < self.similarity_threshold:
                return None, best_score

            if best_query is not None and _looks_like_false_hit(query, best_query):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "matched_key": best_query,
                        "score": best_score,
                        "ts": time.time(),
                    }
                )
                return None, best_score

            return best_response, best_score
        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response as a Redis hash with TTL. Errors are silently swallowed."""
        if _is_uncacheable(query):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            pass

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
