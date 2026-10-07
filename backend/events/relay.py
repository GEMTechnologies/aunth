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

        if published:
            self.db.commit()
            logger.info("outbox.relay.drained", extra={"published": published})
        else:
            self.db.rollback()
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