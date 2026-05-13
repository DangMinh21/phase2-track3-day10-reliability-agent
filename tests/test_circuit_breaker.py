from __future__ import annotations

import threading
import time

import pytest

from reliability_lab.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


def make_breaker(
    failure_threshold: int = 3,
    reset_timeout: float = 0.1,
    success_threshold: int = 1,
) -> CircuitBreaker:
    return CircuitBreaker(
        name="test",
        failure_threshold=failure_threshold,
        reset_timeout_seconds=reset_timeout,
        success_threshold=success_threshold,
    )


def test_starts_closed_and_allows_requests() -> None:
    cb = make_breaker()
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True


def test_opens_after_failure_threshold() -> None:
    cb = make_breaker(failure_threshold=3)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False


def test_call_raises_when_open() -> None:
    cb = make_breaker(failure_threshold=1)
    cb.record_failure()
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: "noop")


def test_transitions_to_half_open_after_reset_timeout() -> None:
    cb = make_breaker(failure_threshold=1, reset_timeout=0.05)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    time.sleep(0.06)
    assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN


def test_half_open_closes_on_success() -> None:
    cb = make_breaker(failure_threshold=1, reset_timeout=0.05, success_threshold=1)
    cb.record_failure()
    time.sleep(0.06)
    cb.allow_request()  # triggers OPEN → HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


def test_half_open_reopens_immediately_on_failure() -> None:
    cb = make_breaker(failure_threshold=3, reset_timeout=0.05)
    for _ in range(3):
        cb.record_failure()
    time.sleep(0.06)
    cb.allow_request()  # → HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_transition_log_captures_full_cycle() -> None:
    cb = make_breaker(failure_threshold=2, reset_timeout=0.05)
    cb.record_failure()
    cb.record_failure()
    time.sleep(0.06)
    cb.allow_request()
    cb.record_success()
    transitions = [(t["from"], t["to"]) for t in cb.transition_log]
    assert ("closed", "open") in transitions
    assert ("open", "half_open") in transitions
    assert ("half_open", "closed") in transitions


def test_call_records_success_on_return() -> None:
    cb = make_breaker(failure_threshold=2)
    assert cb.call(lambda: 42) == 42
    assert cb.failure_count == 0


def test_call_records_failure_on_exception() -> None:
    cb = make_breaker(failure_threshold=2)

    def boom() -> None:
        raise RuntimeError("simulated")

    with pytest.raises(RuntimeError):
        cb.call(boom)
    assert cb.failure_count == 1


def test_no_retry_storm_when_open() -> None:
    """When OPEN, allow_request returns False without touching state."""
    cb = make_breaker(failure_threshold=1, reset_timeout=10.0)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    for _ in range(50):
        assert cb.allow_request() is False
    assert cb.state == CircuitState.OPEN


def test_thread_safe_concurrent_failures() -> None:
    """Concurrent record_failure calls must not corrupt failure_count."""
    cb = make_breaker(failure_threshold=10_000)

    def record_n(n: int) -> None:
        for _ in range(n):
            cb.record_failure()

    threads = [threading.Thread(target=record_n, args=(100,)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cb.failure_count == 1000
