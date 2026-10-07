"""Deliver routed events to a person, from the relay.

THE INTEGRATION THAT MAKES THIS REAL
------------------------------------
Without it, `agent/notifications/` is a library nothing calls - which is the same defect
this round exists to fix, one level up. Every event already flows:

    service -> outbox_events (committed with the work) -> relay -> Redis Streams

The relay is the natural place to route, because it already iterates committed events in
order and it is the only component that sees all of them. Adding the call here means
`report.overdue` finally reaches somebody.

WHY IT IS SAFE TO DO HERE
-------------------------
Routing is **no more consequential than publishing**. It writes a notification row for an
event that has already been committed and is on its way to a stream. An in-app notification
never leaves the platform, and the notification service refuses an external channel outright
- so this cannot become an egress that nobody gated.

And it must not break the relay. A routing failure is caught, logged and counted, exactly
like a publish failure: an event that reached Redis but did not become a notification is a
degraded outcome, and an event that did neither because the notifier threw is a worse one.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)


def recipients_for(db: Session, org_id: str) -> list[str]:
    """Who should hear about this organisation's problems.

    The owner first, because they are accountable, then anyone with an explicit preference
    for a notification category. Discovered rather than configured per event, so a new
    organisation gets notified about its own overdue report without anybody setting
    anything up.

    Returns an empty list when there is nobody - and the notification service then records
    a SUPPRESSED delivery rather than inventing a recipient.
    """
    found: list[str] = []

    owner = db.execute(
        select(models.Organisation.owner_user_id).where(models.Organisation.id == org_id)
    ).scalar()
    if owner:
        found.append(str(owner))

    # Anyone who has expressed a preference is included, so a person who has opted in for
    # a category is not missed just because they are not the owner.
    for (user_id,) in db.execute(
        select(models.NotificationPreference.user_id).where(
            models.NotificationPreference.org_id == org_id,
            models.NotificationPreference.enabled.is_(True),
        ).distinct()
    ).all():
        if user_id and str(user_id) not in found:
            found.append(str(user_id))

    return found


def deliver_event(
    db: Session, *, event: models.OutboxEvent, channels: Optional[list[Any]] = None
) -> Optional[dict[str, Any]]:
    """Route one outbox event to the people who should know.

    Returns the result for a routed event, or None when the event is bookkeeping - which is
    the common case and is not an error.
    """
    from agent.notifications.contract import route_for
    from agent.notifications.service import NotificationService

    if route_for(event.event_type) is None:
        return None

    org_id = event.org_id
    if not org_id:
        # A cross-tenant event with no organisation has no audience. Recorded rather than
        # guessed at.
        logger.debug("notification.no_org_for_event", extra={"event_id": event.id})
        return None

    recipients = recipients_for(db, org_id)
    payload = dict(event.payload or {})
    payload.setdefault("event_id", event.id)

    service = NotificationService(
        db, org_id=org_id, channels=list(channels or _default_channels()), default_recipients=recipients
    )

    # One outcome per recipient, reported together so the caller can see a partial failure
    # rather than only the first.
    results = []
    for user_id in recipients or [None]:
        result = service.raise_for_event(
            event_type=event.event_type,
            payload=payload,
            recipients=[user_id] if user_id else [],
        )
        results.append(result.as_dict())

    return {
        "event_id": event.id,
        "event_type": event.event_type,
        "recipients": recipients,
        "results": results,
    }


def _default_channels() -> list[Any]:
    """In-app only.

    Deliberately internal by default. An operator who wants email notifications must route
    them through the gated outbound path, and the notification service refuses a channel
    that reports itself external - so the default cannot become an ungated egress.
    """
    from agent.notifications.providers.fake import InAppChannel

    return [InAppChannel()]
