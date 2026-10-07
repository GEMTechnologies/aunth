"""Decision telemetry: counters, a circuit breaker, and a latency clock.

The brief requires a named set of decision signals, and the reason is that a
decision layer which cannot be observed cannot be trusted with authority:

    decisions today, provider failures, fallback rate, low-confidence rate,
    average confidence, latency, human escalations, provider agreement.

All of those live in ``observability.metrics`` rather than a private registry, so
one dashboard reads one source of truth instead of three.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from observability import metrics

logger = logging.getLogger(__name__)

#: Every signal this module emits, declared so a dashboard can be written against
#: fixed names and a typo becomes a test failure rather than a flat zero.
DECISION_METRICS = frozenset(
    {
        "decision.requested",
        "decision.completed",
        "decision.failed",
        "decision.low_confidence",
        "decision.escalated",
        "decision.shadowed",
        "decision.fallback_used",
        "decision.cache_hit",
        "decision.cache_miss",
        "decision.policy_blocked",
        "decision.agreement",
        "decision.disagreement",
        "decision.confidence_sum",
        "decision.latency_ms",
        "decision.provider_unavailable",
        "decision.circuit_open",
    }
)


class Clock:
    """Measures one attempt.

    ``perf_counter``, not ``monotonic``: on Windows ``monotonic`` has ~15 ms
    granularity, so a fast rules decision - which is most of them - would record
    exactly zero latency. See ``agent.matching`` and ADR-0008 for the measurement.
    """

    __slots__ = ("started", "elapsed_ms")

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.elapsed_ms = 0

    def stop(self) -> int:
        self.elapsed_ms = int((time.perf_counter() - self.started) * 1000)
        return self.elapsed_ms


class timed:
    """Context manager yielding a :class:`Clock`, stopped on the way out.

    Stops on the exception path too, because a failed call's latency is exactly
    the number you want during an incident.
    """

    def __enter__(self) -> Clock:
        self._clock = Clock()
        return self._clock

    def __exit__(self, *exc: Any) -> bool:
        self._clock.stop()
        return False


@dataclass
class CircuitBreaker:
    """Stops hammering a provider that is down.

    Without this, an outage turns every decision into a timeout and the platform's
    throughput collapses even though the decision layer is optional. Half-open
    after the cooldown, so recovery is detected without a human restarting
    anything.
    """

    failure_threshold: int = 5
    cooldown_seconds: int = 60
    _failures: int = 0
    _opened_at: Optional[datetime] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if datetime.now(timezone.utc) - self._opened_at >= timedelta(seconds=self.cooldown_seconds):
                # Cooldown elapsed: allow one attempt through (half-open).
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold and self._opened_at is None:
                self._opened_at = datetime.now(timezone.utc)
                metrics.inc("decision.circuit_open")

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None


def record_decision(result: Any, *, shadow: bool, fallback: bool) -> None:
    """Emit the counters for one completed decision."""
    metrics.inc("decision.completed", provider=result.provider, decision_type=result.decision_type)
    metrics.observe("decision.latency_ms", float(result.latency_ms), provider=result.provider)
    if fallback:
        metrics.inc("decision.fallback_used", provider=result.provider)
    if shadow:
        metrics.inc("decision.shadowed", decision_type=result.decision_type)
    if result.confidence is not None:
        metrics.inc("decision.confidence_sum", float(result.confidence))
    else:
        # An absent confidence is counted as low rather than skipped, so the
        # low-confidence rate cannot be improved by a provider that stops
        # reporting one.
        metrics.inc("decision.low_confidence", provider=result.provider)
