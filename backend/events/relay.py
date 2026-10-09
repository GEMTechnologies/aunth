"""Outbox relay: moves committed events from PostgreSQL to Redis Streams.

The invariant
-------------
An event is published only *after* the transaction that produced it has
committed, and never if that transaction rolled back. That is the whole point
of the transactional outbox, and it is why this is a separate process rather
than a call inside the request handler.

The relay is therefore deliberately cross-tenant. It drains unpublished rows
for every organisation in one sweep and has no tenant of its own to set. Its
PostgreSQL access runs as the owner against an ENABLE-only table
(``outbox_events``, see migration 004); the application role stays bound by
the policies and cannot use this path.

Delivery is at-least-once. Rows are marked published only after ``XADD``
returns, so a crash between the two replays the event. Consumers are therefore
required to be idempotent - they dedupe on the ledger's idempotency key, not
on the fact of having received something.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from agent import heartbeat
from events.publisher import RedisEventPublisher

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OutboxRelay:
    """Publishes committed outbox rows to Redis Streams."""

    def __init__(
        self,
        db: Session,
        publisher: RedisEventPublisher,
        *,
        batch_size: int = 100,
        max_attempts: int = 10,
    ) -> None:
        self.db = db
        self.publisher = publisher
        self.batch_size = batch_size
        self.max_attempts = max_attempts

        #: How many events became notifications, and how many failed to. Reported by the
        #: daemon's log line, because a notifier that has silently stopped working looks
        #: exactly like a system with nothing to report.
        self.notified = 0
        self.notify_failures = 0

    def pending(self) -> list[models.OutboxEvent]:
        """Unpublished rows, oldest first.

        ``with_for_update(skip_locked=True)`` lets several relays run
        concurrently without processing the same row twice: the loser simply
        skips rows the winner is holding.
        """
        return list(
            self.db.execute(
                select(models.OutboxEvent)
                .where(models.OutboxEvent.published_at.is_(None))
                .order_by(models.OutboxEvent.created_at)
                .limit(self.batch_size)
                .with_for_update(skip_locked=True)
            ).scalars()
        )

    def drain_once(self) -> int:
        """Publish one batch. Returns how many were published.

        The caller owns the transaction. Publishing marks ``published_at`` and
        commits together, so the at-least-once window is bounded by the
        distance between ``XADD`` returning and the commit - never by a
        half-written state.
        """
        published = 0
        for event in self.pending():
            if event.attempts >= self.max_attempts:
                # Stop hammering. The row stays unpublished and visible, which
                # is more useful than deleting it: an operator can see what
                # failed and why instead of finding a silently missing event.
                logger.error(
                    "outbox.relay.abandoned",
                    extra={
                        "event_id": event.id,
                        "attempts": event.attempts,
                        "last_error": event.last_error,
                    },
                )
                continue
            try:
                self.publisher.publish_raw(
                    stream=event.stream,
                    fields=self._fields(event),
                )
            except Exception as exc:  # noqa: BLE001 - must not kill the sweep
                event.attempts += 1
                event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                logger.warning(
                    "outbox.relay.publish_failed",
                    extra={
                        "event_id": event.id,
                        "attempt": event.attempts,
                        "error": event.last_error,
                    },
                )
                continue

            event.published_at = _now()
            event.attempts += 1
            event.last_error = None
            published += 1

            # Route it to the people who should know. This is the step that was missing:
            # `report.overdue` reached Redis and stopped there, so the alert with the
            # clearest financial consequence was published perfectly and read by nobody.
            #
            # A routing failure must not fail the publish above, for the same reason a
            # publish failure must not kill the sweep: the event HAS reached the stream,
            # and converting a delivered event into a retried one would duplicate it. It is
            # counted and logged instead.
            try:
                from agent.notifications.integration import deliver_event

                                # `self.db`, not `self.session`. The wrong attribute compiles cleanly
                # and fails at the first routed event - the same class of defect as the
                # undefined `_agent_for` helper in the delivery routes.
                outcome = deliver_event(self.db, event=event)
                if outcome:
                    self.notified += 1
                    logger.info(
                        "outbox.relay.notified",
                        extra={
                            "event_id": event.id,
                            "event_type": event.event_type,
                            "recipients": len(outcome.get("recipients") or []),
                        },
                    )
            except Exception as exc:  # noqa: BLE001 - never break the sweep
                self.notify_failures += 1
                logger.warning(
                    "outbox.relay.notify_failed",
                    extra={
                        "event_id": event.id,
                        "event_type": event.event_type,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    },
                )

        if published:
            self.db.commit()
            logger.info("outbox.relay.drained", extra={"published": published})
        else:
            # COMMIT the failure bookkeeping, and do not roll it back.
            #
            # This was a real defect: the previous version rolled back when
            # nothing published, which discarded the `attempts += 1` and
            # `last_error` it had just recorded. Two consequences, both bad.
            # `max_attempts` became unreachable for publish failures, so the
            # "stop hammering and log abandoned" path could never fire during an
            # outage; and an operator inspecting the outbox saw attempts=0 for an
            # event that had been failing for hours, which reads as "fine".
            #
            # The rollback was presumably there to avoid committing a partial
            # batch, but a failed publish changes nothing except the counters -
            # and those are exactly what must survive.
            self.db.commit()
            logger.warning(
                "outbox.relay.nothing_published",
                extra={"pending": len(self.pending())},
            )
        return published

    def _fields(self, event: models.OutboxEvent) -> dict[str, Any]:
        return {
            "event_id": event.id,
            "event_type": event.event_type,
            "org_id": event.org_id or "",
            "trace_id": event.trace_id or "",
            "created_at": event.created_at.isoformat() if event.created_at else "",
            "payload": event.payload or {},
        }


class Inbox:
    """Webhook dedupe on the inbound side.

    Providers redeliver. Gmail retries a failed webhook for days, and a
    duplicate award notification handled twice would hand one grant to two
    workers. The unique constraint on ``(source, external_event_id)`` turns
    that race into a database error rather than a duplicated business action.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def claim(
        self,
        *,
        source: str,
        external_event_id: str,
        payload: Optional[dict[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> tuple[Optional[models.InboxEvent], bool]:
        """Record an inbound event. Returns ``(event, is_new)``.

        ``is_new`` is False for a redelivery, and the caller must then do
        nothing. This is checked as a uniqueness violation rather than a
        SELECT-then-INSERT, because two concurrent deliveries of the same
        webhook would both pass a SELECT and both insert.
        """
        existing = self.db.execute(
            select(models.InboxEvent).where(
                models.InboxEvent.source == source,
                models.InboxEvent.external_event_id == external_event_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

        event = models.InboxEvent(
            source=source,
            external_event_id=external_event_id,
            payload=payload or {},
            org_id=org_id,
            status=models.InboxEvent.RECEIVED,
            received_at=_now(),
        )
        try:
            # A SAVEPOINT, not the outer transaction. Losing the race against a
            # concurrent delivery must discard only this INSERT - a bare
            # db.rollback() here would silently throw away whatever the request
            # had already written (an AuditLog row, a mailbox sync cursor) and
            # the caller would never learn why.
            #
            # The add() belongs inside the block: SQLAlchemy binds pending
            # objects to the transaction that owns them, so staging the row
            # first and wrapping only the flush leaves the failed row attached
            # to the outer transaction and leaves it DEACTIVE afterwards.
            with self.db.begin_nested():
                self.db.add(event)
                self.db.flush()
        except IntegrityError:
            # Lost the race. The savepoint is already rolled back, so the
            # caller's earlier work in this transaction survives.
            existing = self.db.execute(
                select(models.InboxEvent).where(
                    models.InboxEvent.source == source,
                    models.InboxEvent.external_event_id == external_event_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                # The constraint fired for some reason other than our duplicate
                # (a truncated identifier, say). Returning a silent no-op here
                # would drop the webhook on the floor, so surface it instead.
                raise
            return existing, False
        return event, True

    def mark(self, event: models.InboxEvent, *, status: str, note: Optional[str] = None) -> None:
        event.status = status
        event.note = note
        if status in (models.InboxEvent.PROCESSED, models.InboxEvent.IGNORED):
            event.processed_at = _now()
        self.db.flush()

# ---------------------------------------------------------------------------
# Runnable entry point (Phase 7b, task 0A)
# ---------------------------------------------------------------------------
# Until now the relay had NO main(). The systemd unit referenced
# `python -m events.relay` and that command did not exist, which is a
# particularly bad class of gap: the unit looked complete, the documentation said
# the relay was deployable, and nothing would ever have published an event. It was
# found by asking what the unit actually invokes.
#
# The same shape as FleetRunner on purpose: an interval, a session per sweep, a
# sliced sleep so SIGTERM is honoured promptly, failure that is counted rather than
# fatal, and a health snapshot. Two long-running processes that behave differently
# under shutdown is a maintenance liability, not a design choice.
class RelayHealth:
    """What an operator or a probe needs to know about the relay."""

    def __init__(self) -> None:
        self.running = False
        self.stopping = False
        self.sweeps = 0
        self.published = 0
        self.errors = 0
        self.consecutive_empty = 0
        self.last_sweep_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.started_at: Optional[datetime] = None

    @property
    def healthy(self) -> bool:
        return self.running and not self.stopping and self.sweeps > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "stopping": self.stopping,
            "healthy": self.healthy,
            "sweeps": self.sweeps,
            "published": self.published,
            "errors": self.errors,
            "consecutive_empty": self.consecutive_empty,
            "last_sweep_at": self.last_sweep_at.isoformat() if self.last_sweep_at else None,
            "last_error": self.last_error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }


#: How often to drain. Short enough that a dispatch becomes a wake-up promptly,
#: long enough that the query is not the busiest thing in the system.
DEFAULT_RELAY_INTERVAL_SECONDS = 5

#: Cap on a single sleep slice, so a stop signal is honoured within a second even
#: if the interval is configured long.
MAX_SLEEP_SLICE_SECONDS = 1.0


class OutboxRelayRunner:
    """Drains the outbox on an interval, forever, until asked to stop."""

    def __init__(
        self,
        session_factory: Any,
        publisher: Optional[RedisEventPublisher] = None,
        *,
        interval_seconds: float = DEFAULT_RELAY_INTERVAL_SECONDS,
        batch_size: int = 100,
    ) -> None:
        import threading

        self.session_factory = session_factory
        self.publisher = publisher or RedisEventPublisher()
        self.interval_seconds = max(0.5, float(interval_seconds))
        self.batch_size = batch_size
        self.health = RelayHealth()
        self._stop = threading.Event()

    def sweep_once(self) -> int:
        """One drain in its own session and transaction.

        The session factory call is INSIDE the guard. It was outside in the fleet
        runner, and the result was a dispatcher that looked healthy while having
        silently done nothing all night; the same mistake here would leave the
        outbox full and the health snapshot clean.
        """
        db = None
        try:
            db = self.session_factory()
            relay = OutboxRelay(db, self.publisher, batch_size=self.batch_size)
            published = relay.drain_once()   # commits the bookkeeping itself
            self.health.sweeps += 1
            self.health.published += published
            self.health.consecutive_empty = 0 if published else self.health.consecutive_empty + 1
            self.health.last_sweep_at = _now()
            return published
        except Exception as exc:
            if db is not None:
                try:
                    db.rollback()
                except Exception:  # pragma: no cover - connection already gone
                    pass
            self.health.errors += 1
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            # An unreachable Redis must not kill the relay: the outbox is durable and
            # the next sweep is the recovery path. Restarting the process instead
            # would work too, but only because systemd would restart it - the loop
            # continuing is the simpler guarantee.
            logger.warning("outbox.relay.sweep_failed", extra={"error": self.health.last_error})
            raise
        finally:
            if db is not None:
                db.close()

    def run_forever(self, *, max_sweeps: Optional[int] = None) -> RelayHealth:
        self.health.running = True
        self.health.started_at = _now()
        self._stop.clear()
        logger.info(
            "outbox.relay.started",
            extra={"interval_seconds": self.interval_seconds, "batch_size": self.batch_size},
        )
        try:
            while not self._stop.is_set():
                try:
                    self.sweep_once()
                except Exception:
                    pass  # counted and logged; the loop continues

                # See agent/heartbeat.py: the relay listens on no port, so Docker's HTTP
                # healthcheck could never succeed for this service. A completed sweep is what
                # "alive" means here.
                heartbeat.beat()

                if max_sweeps is not None and self.health.sweeps >= max_sweeps:
                    break
                self._sleep()
        finally:
            self.health.running = False
            logger.info("outbox.relay.stopped", extra=self.health.as_dict())
        return self.health

    def _sleep(self) -> None:
        remaining = self.interval_seconds
        while remaining > 0 and not self._stop.is_set():
            slice_seconds = min(MAX_SLEEP_SLICE_SECONDS, remaining)
            if self._stop.wait(slice_seconds):
                return
            remaining -= slice_seconds

    def stop(self) -> None:
        self.health.stopping = True
        self._stop.set()

    def request_stop(self, *_args: Any) -> None:
        """Signal-handler shaped."""
        self.stop()


def install_signal_handlers(runner: OutboxRelayRunner) -> bool:
    """Wire SIGTERM/SIGINT to a graceful stop. Returns whether it succeeded."""
    import signal as _signal

    installed = False
    for name in ("SIGTERM", "SIGINT"):
        number = getattr(_signal, name, None)
        if number is None:
            continue
        try:
            _signal.signal(number, runner.request_stop)
            installed = True
        except (ValueError, OSError):  # not the main thread
            return installed
    return installed


def main() -> int:  # pragma: no cover - process entry point
    """``python -m events.relay`` - the command the systemd unit invokes."""
    from config import settings
    # See agent/fleet_runner.py and ADR-0011: the relay claims `outbox_events` unscoped, and that
    # table is FORCE ROW LEVEL SECURITY, so the application role reads zero rows and the relay
    # silently publishes nothing. Same credential, same reason.
    from database import FleetSessionLocal
    from observability import configure_logging, register_secrets_from_settings

    configure_logging(level=getattr(settings, "log_level", "INFO"), service="granada-outbox-relay")
    register_secrets_from_settings(settings)

    runner = OutboxRelayRunner(
        FleetSessionLocal,
        interval_seconds=float(getattr(settings, "outbox_relay_interval_seconds", DEFAULT_RELAY_INTERVAL_SECONDS)),
        batch_size=int(getattr(settings, "outbox_relay_batch_size", 100)),
    )
    install_signal_handlers(runner)
    runner.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
