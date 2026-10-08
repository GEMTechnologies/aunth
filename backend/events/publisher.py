"""Event publication over Redis Streams.

Scope note
----------
Redis is the transport here, never the record of what happened. Every event
is also written to PostgreSQL by the caller's own transaction, so a lost or
trimmed stream entry cannot erase the fact that an opportunity was
discovered or an application was submitted. Streams are a delivery
mechanism; the durable truth stays in the database.

Key names follow ``granada:v1:<domain>:<action>`` so operators can grep a
running system for a single event type.

This module performs no connection work at import time, so importing it
costs nothing and a missing Redis does not break application start-up.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

NAMESPACE = "granada:v1"

# Retry classification. A consumer routes failures by this, never by string
# matching the exception message.
RETRY_CATEGORIES = frozenset(
    {
        "TRANSIENT_NETWORK",
        "RATE_LIMITED",
        "AUTH_EXPIRED",
        "VALIDATION_ERROR",
        "POLICY_BLOCKED",
        "HUMAN_REQUIRED",
        "REMOTE_CHANGED",
        "PERMANENT_REJECTION",
    }
)


class EventPublisherError(RuntimeError):
    """Raised when an event could not be handed to Redis."""


@dataclass(frozen=True)
class JobEnvelope:
    """The unit of work a worker receives.

    ``idempotency_key`` is what makes redelivery safe: a worker must refuse
    to perform work it has already recorded under that key.
    """

    job_id: str
    workflow_id: str
    organization_id: Optional[str]
    job_type: str
    idempotency_key: str
    payload_ref: Optional[str] = None
    product_context: Optional[str] = None
    attempt: int = 1
    max_attempts: int = 5
    not_before: Optional[str] = None
    trace_id: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def stream_name(domain: str, action: str) -> str:
    """Build the canonical stream key, e.g. ``granada:v1:mail:received``."""
    domain = domain.strip().strip(":").lower()
    action = action.strip().strip(":").lower()
    if not domain or not action:
        raise ValueError("domain and action are both required")
    return f"{NAMESPACE}:{domain}:{action}"


def rate_limit_key(action: str, identifier: str) -> str:
    """Per-tenant, per-action rate limit key."""
    return f"{NAMESPACE}:ratelimit:{action.strip().strip(':').lower()}:{identifier}"


def _encode(fields: dict[str, Any]) -> dict[str, str]:
    """Redis stream fields are flat strings; nested values become JSON."""
    encoded: dict[str, str] = {}
    for key, value in fields.items():
        if value is None:
            encoded[key] = ""
        elif isinstance(value, (dict, list)):
            encoded[key] = json.dumps(value, separators=(",", ":"), sort_keys=True)
        elif isinstance(value, datetime):
            encoded[key] = value.isoformat()
        else:
            encoded[key] = str(value)
    return encoded


#: Seconds a single Redis SOCKET operation may take before it is abandoned.
#:
#: redis-py defaults this and `socket_connect_timeout` to **None**, which means *block forever*.
#: The default is fine for a script and wrong for a service: a Redis that accepts a connection and
#: then stops answering - which is what a wedged instance does under memory pressure, and what a
#: restart looks like from the client side - would block `xadd` in the relay, `xreadgroup` in the
#: fleet, and `ping` behind `/readyz`, none of which raise.
#:
#: Five seconds is far longer than any healthy Redis operation and far shorter than an outage.
SOCKET_TIMEOUT_SECONDS = 5.0

#: Seconds to wait for the TCP connection itself. Shorter, because a host that is not answering at
#: all should be given up on quickly.
SOCKET_CONNECT_TIMEOUT_SECONDS = 3.0


class RedisEventPublisher:
    """Publishes versioned events to Redis Streams.

    The client is created on first use so that importing this module does not
    require Redis to be reachable.
    """

    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url or os.environ.get("REDIS_URL") or "redis://localhost:6379/0"
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import redis  # imported lazily on purpose
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise EventPublisherError(
                    "redis package is not installed; cannot publish events"
                ) from exc
            self._client = redis.Redis.from_url(
                self._url,
                decode_responses=True,
                # Without these two, every operation on this client can block forever. See the
                # note on SOCKET_TIMEOUT_SECONDS.
                socket_timeout=SOCKET_TIMEOUT_SECONDS,
                socket_connect_timeout=SOCKET_CONNECT_TIMEOUT_SECONDS,
                # A timeout must SURFACE, not be retried indefinitely. The relay already handles
                # an unreachable Redis by leaving the outbox durable; an infinite retry would
                # reintroduce the block it is meant to escape.
                retry_on_timeout=False,
            )
        return self._client

    def publish(
        self,
        domain: str,
        action: str,
        envelope: JobEnvelope,
        extra: Optional[dict[str, Any]] = None,
    ) -> str:
        """Publish one event and return the generated stream entry id."""
        fields = _encode({**envelope.to_dict(), **(extra or {})})
        key = stream_name(domain, action)

        try:
            entry_id = self.client.xadd(key, fields, maxlen=100_000, approximate=True)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            raise EventPublisherError(f"failed to publish to {key}: {exc}") from exc

        logger.info(
            "event published",
            extra={"stream": key, "entry_id": entry_id, "job_id": envelope.job_id},
        )
        return entry_id

    def publish_raw(self, stream: str, fields: dict[str, Any]) -> str:
        """Publish an already-shaped event to a named stream.

        Added for the transactional outbox relay, which holds generic events
        that are not job envelopes (a mailbox synced, an eligibility gate
        evaluated). ``publish()`` keeps its stricter ``JobEnvelope`` signature;
        this method is deliberately the looser escape hatch rather than
        weakening that one, because every job-shaped caller depends on the
        envelope's fields being guaranteed present.

        Same encoding, same trimming, same failure semantics as ``publish``.
        """
        encoded = _encode(fields)
        try:
            entry_id = self.client.xadd(stream, encoded, maxlen=100_000, approximate=True)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            raise EventPublisherError(
                f"failed to publish to {stream}: {exc}"
            ) from exc

        logger.info(
            "event published",
            extra={"stream": stream, "entry_id": entry_id, "event_type": fields.get("event_type")},
        )
        return entry_id

    def ping(self) -> bool:
        """True when Redis answers. Used by health checks, never by request paths."""
        try:
            return bool(self.client.ping())
        except Exception:  # noqa: BLE001
            return False


_publisher: Optional[RedisEventPublisher] = None


def get_publisher() -> RedisEventPublisher:
    """Process-wide publisher instance."""
    global _publisher
    if _publisher is None:
        _publisher = RedisEventPublisher()
    return _publisher