"""Turning events into notifications a person can act on.

The suppression rule is the whole design
----------------------------------------
The fleet and the relay work on a timer, so one overdue report emits an event on every
scan. Without suppression that is thousands of notifications a day for one problem, the
channel gets muted, and the next real problem is missed. **A notification system that
floods is worse than none**, because it trains people to ignore the channel that would have
told them.

So ``dedupe_key`` identifies the SUBJECT and the CONDITION - ``report:{id}:overdue`` - never
the event or its timestamp. Re-raising an open notification **increments a repeat count**
instead of creating a second row, because a reminder is not a new item and the count is what
tells a person it is not going away.

Three refusals
--------------
1. **A closed notification is never resurrected.** Once dismissed or actioned, the same
   condition does not come back through this path. If the condition genuinely recurs, that is
   a new decision and it should be visible as one - not a silent re-open.
2. **An external channel is refused.** An email notification reaches outside the platform
   in the organisation's name, and the Phase 7b outbound path already gates exactly that.
   A second egress is a second place to audit, and the one that gets forgotten is the one
   that sends.
3. **Quiet hours defer rather than drop.** A funder deadline is not urgent at 03:00, and a
   notification that wakes somebody for a non-urgent item is one they turn off - but it is
   deferred, not discarded, so nothing is lost to a sleep schedule.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from agent.notifications.contract import (
    Category,
    DeliveryOutcome,
    NotificationChannel,
    NotificationChannelError,
    RaiseResult,
    Route,
    Severity,
    format_route,
    route_for,
)

logger = logging.getLogger(__name__)

#: How long before the same condition may raise a NEW notification once the previous one
#: was closed. Within the window it is a repeat of the open one; after it, a closed
#: notification stays closed and this path stays quiet - see refusal 1.
REPEAT_WINDOW_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class NotificationService:
    """Routes events to people, suppresses duplicates, records delivery."""

    def __init__(
        self,
        db: Session,
        *,
        org_id: str,
        channels: Optional[Iterable[NotificationChannel]] = None,
        default_recipients: Optional[Iterable[str]] = None,
    ) -> None:
        if not org_id:
            raise ValueError("notifications require an organisation")
        self.db = db
        self.org_id = org_id
        #: In-app only by default. An external channel has to be passed explicitly, and is
        #: still refused at delivery time unless routed through the gate.
        self.channels: list[NotificationChannel] = list(channels or ())
        #: Who to tell when nothing more specific is configured. An organisation with no
        #: owner configured gets no notification, which is honest - the alternative is
        #: inventing a recipient.
        self.default_recipients = list(default_recipients or ())

    # ==================================================================
    # Raising
    # ==================================================================
    def raise_for_event(
        self, *, event_type: str, payload: dict[str, Any], recipients: Optional[Iterable[str]] = None
    ) -> RaiseResult:
        """Route one event, creating, repeating or suppressing as appropriate."""
        route = route_for(event_type)
        if route is None:
            # THE COMMON CASE AND IT IS DELIBERATE. Most events are bookkeeping; raising a
            # notification for each would bury the ones that matter.
            return RaiseResult(
                notification_id=None, action="UNROUTED",
                reason=f"no route for {event_type}; nothing is raised for bookkeeping events",
            )

        missing = [f for f in route.required_fields if payload.get(f) is None]
        if missing:
            # Reported rather than raised: a routing table that has drifted from the
            # emitter should be visible, and a notification path that throws is one that
            # fails silently at exactly the wrong moment.
            logger.warning(
                "notification.route_missing_fields",
                extra={"event_type": event_type, "missing": missing},
            )

        title, body, dedupe_key = format_route(route, payload)
        audience = list(recipients or self.default_recipients)
        if not audience:
            return RaiseResult(
                notification_id=None, action="SUPPRESSED",
                reason="no recipient is configured; inventing one would notify a stranger",
            )

        results: list[RaiseResult] = []
        for user_id in audience:
            results.append(
                self._raise_for_recipient(
                    route=route, title=title, body=body, dedupe_key=dedupe_key,
                    user_id=user_id, event_type=event_type, payload=payload,
                )
            )
        # Report the first recipient's outcome; a caller raising to several can inspect
        # each by calling per recipient.
        return results[0] if results else RaiseResult(
            notification_id=None, action="SUPPRESSED", reason="no recipients"
        )

    def _raise_for_recipient(
        self,
        *,
        route: Route,
        title: str,
        body: str,
        dedupe_key: str,
        user_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> RaiseResult:
        existing = self.db.execute(
            select(models.Notification).where(
                models.Notification.org_id == self.org_id,
                models.Notification.user_id == user_id,
                models.Notification.dedupe_key == dedupe_key,
            )
        ).scalars().first()

        if existing is not None:
            # REFUSAL 1: a closed notification is never resurrected. Re-opening it would
            # hide that somebody already dealt with it, and a condition that genuinely
            # recurs deserves to be visible as a new decision.
            if existing.status not in models.Notification.OPEN_STATUSES:
                return RaiseResult(
                    notification_id=existing.id, action="SUPPRESSED",
                    reason=(
                        f"the notification for this condition was already "
                        f"{existing.status.lower()} and is not re-opened; a recurrence is a "
                        "new decision and should be visible as one"
                    ),
                )
            # A REPEAT, not a new item. The count is what tells a person it is not going
            # away, and it keeps one problem as one row.
            existing.repeat_count = (existing.repeat_count or 0) + 1
            existing.last_raised_at = _now()
            # The body is refreshed because the figures in it move - days remaining, an
            # amount - while the identity of the condition does not.
            existing.body = body
            self.db.flush()
            return RaiseResult(
                notification_id=existing.id, action="REPEATED",
                reason=f"raised {existing.repeat_count} time(s) in total; still open",
                deliveries=self._deliver(existing, user_id),
            )

        notification = models.Notification(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=payload.get("agent_id"),
            user_id=user_id,
            category=route.category.value,
            severity=route.severity.value,
            status=models.Notification.STATUS_UNREAD,
            title=title,
            body=body,
            action_required=route.action_required,
            action_url=route.action_url.format_map(
                _Tolerant(payload)
            ) if route.action_url else None,
            dedupe_key=dedupe_key,
            source_event_type=event_type,
            source_event_id=payload.get("event_id"),
            context={k: v for k, v in payload.items() if k != "event_id"},
            created_at=_now(),
            repeat_count=0,
            last_raised_at=_now(),
        )
        self.db.add(notification)
        try:
            self.db.flush()
        except IntegrityError:
            # A concurrent raiser won the unique constraint. That is the suppression
            # working, not an error: re-read and treat it as a repeat.
            self.db.rollback()
            concurrent = self.db.execute(
                select(models.Notification).where(
                    models.Notification.org_id == self.org_id,
                    models.Notification.user_id == user_id,
                    models.Notification.dedupe_key == dedupe_key,
                )
            ).scalars().first()
            if concurrent is None:  # pragma: no cover - defensive
                raise
            concurrent.repeat_count = (concurrent.repeat_count or 0) + 1
            concurrent.last_raised_at = _now()
            self.db.flush()
            return RaiseResult(
                notification_id=concurrent.id, action="REPEATED",
                reason="another raiser created this first; treated as a repeat",
            )

        return RaiseResult(
            notification_id=notification.id, action="CREATED",
            deliveries=self._deliver(notification, user_id),
        )

    # ==================================================================
    # Delivery
    # ==================================================================
    def _deliver(self, notification: models.Notification, user_id: str) -> list[DeliveryOutcome]:
        """Put the notification on each enabled channel, recording every attempt."""
        outcomes: list[DeliveryOutcome] = []
        severity = Severity(notification.severity)

        preferences = self.db.execute(
            select(models.NotificationPreference).where(
                models.NotificationPreference.org_id == self.org_id,
                models.NotificationPreference.user_id == user_id,
            )
        ).scalars().all()
        by_channel = {p.channel: p for p in preferences}

        if not self.channels:
            # Nothing to deliver to. Recorded rather than ignored, because "the platform
            # knew and told nobody" is a different fact from "it told somebody and they
            # have not looked".
            outcomes.append(
                self._record(
                    notification, user_id, channel="NONE", result="SUPPRESSED",
                    reason="no channel is configured for this deployment",
                )
            )
            return outcomes

        for channel in self.channels:
            preference = by_channel.get(channel.name)

            if preference is not None and not preference.enabled:
                outcomes.append(
                    self._record(
                        notification, user_id, channel=channel.name, result="SUPPRESSED",
                        reason="this person disabled this channel for this category",
                    )
                )
                continue
            if preference is not None:
                try:
                    if not severity.at_least(Severity(preference.min_severity)):
                        outcomes.append(
                            self._record(
                                notification, user_id, channel=channel.name,
                                result="SUPPRESSED",
                                reason=(
                                    f"{severity.value} is below this channel's threshold "
                                    f"of {preference.min_severity}"
                                ),
                            )
                        )
                        continue
                except ValueError:
                    pass

            # REFUSAL 2: an external channel is refused here. It reaches a person outside
            # the platform in the organisation's name, and the outbound mail path already
            # gates exactly that.
            if not getattr(channel, "internal", False):
                outcomes.append(
                    self._record(
                        notification, user_id, channel=channel.name, result="SUPPRESSED",
                        reason=(
                            "external channels must go through the gated outbound path "
                            "(agent.mail.send_service); this service will not deliver "
                            "outside the platform"
                        ),
                        error_code="EXTERNAL_CHANNEL_REFUSED",
                    )
                )
                continue

            # REFUSAL 3: quiet hours DEFER rather than drop.
            if preference is not None and self._in_quiet_hours(preference):
                outcomes.append(
                    self._record(
                        notification, user_id, channel=channel.name, result="DEFERRED",
                        reason=(
                            f"quiet hours {preference.quiet_from_hour}:00-"
                            f"{preference.quiet_to_hour}:00; deferred, not discarded"
                        ),
                    )
                )
                continue

            try:
                outcome = channel.deliver(notification=notification, recipient=user_id)
            except NotificationChannelError as exc:
                outcomes.append(
                    self._record(
                        notification, user_id, channel=channel.name, result="FAILED",
                        reason=str(exc)[:255], error_code=type(exc).__name__,
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001
                outcomes.append(
                    self._record(
                        notification, user_id, channel=channel.name, result="FAILED",
                        reason=f"{type(exc).__name__}: {exc}"[:255],
                        error_code="UNCLASSIFIED",
                    )
                )
                continue

            outcomes.append(
                self._record(
                    notification, user_id, channel=channel.name, result=outcome.result,
                    reason=outcome.reason, provider_reference=outcome.provider_reference,
                )
            )
        return outcomes

    @staticmethod
    def _in_quiet_hours(preference: models.NotificationPreference) -> bool:
        """Whether now falls in the do-not-disturb window.

        Handles a window that crosses midnight, which is the common one - 22:00 to 07:00 -
        and which a naive `from <= hour < to` gets exactly backwards.
        """
        if preference.quiet_from_hour is None or preference.quiet_to_hour is None:
            return False
        hour = _now().hour
        start, end = preference.quiet_from_hour, preference.quiet_to_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _record(
        self,
        notification: models.Notification,
        user_id: str,
        *,
        channel: str,
        result: str,
        reason: Optional[str] = None,
        provider_reference: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> DeliveryOutcome:
        self.db.add(
            models.NotificationDelivery(
                id=str(uuid.uuid4()),
                org_id=self.org_id,
                notification_id=notification.id,
                user_id=user_id,
                channel=channel,
                result=result,
                reason=reason[:255] if reason else None,
                provider_reference=provider_reference,
                error_code=error_code,
                attempted_at=_now(),
            )
        )
        self.db.flush()
        return DeliveryOutcome(
            channel=channel, result=result, reason=reason,
            provider_reference=provider_reference, error_code=error_code,
        )

    # ==================================================================
    # The reader's side
    # ==================================================================
    def inbox(
        self, *, user_id: str, unread_only: bool = True, limit: int = 50
    ) -> list[models.Notification]:
        """What this person should see, most urgent first then newest.

        Ordered by severity before recency, because a critical notification raised
        yesterday matters more than an informational one raised a minute ago - and a
        chronological inbox buries it under the noise that arrived since.
        """
        statement = select(models.Notification).where(
            models.Notification.org_id == self.org_id,
            models.Notification.user_id == user_id,
        )
        if unread_only:
            statement = statement.where(
                models.Notification.status.in_(tuple(models.Notification.OPEN_STATUSES))
            )
        rows = self.db.execute(statement.limit(max(1, min(limit, 500)))).scalars().all()
        order = {Severity.CRITICAL.value: 0, Severity.WARNING.value: 1, Severity.INFO.value: 2}
        return sorted(rows, key=lambda n: (order.get(n.severity, 9), -n.created_at.timestamp()))

    def mark(self, *, notification_id: str, user_id: str, status: str) -> models.Notification:
        """Read, action or dismiss. Never delete."""
        allowed = {
            models.Notification.STATUS_READ,
            models.Notification.STATUS_ACTIONED,
            models.Notification.STATUS_DISMISSED,
        }
        if status not in allowed:
            raise ValueError(f"{status} is not a valid notification status")
        notification = self.db.execute(
            select(models.Notification).where(
                models.Notification.id == notification_id,
                models.Notification.org_id == self.org_id,
                models.Notification.user_id == user_id,
            )
        ).scalars().first()
        if notification is None:
            raise ValueError(f"no notification {notification_id} for this person")
        notification.status = status
        if status == models.Notification.STATUS_READ and notification.read_at is None:
            notification.read_at = _now()
        self.db.flush()
        return notification

    def summary(self, *, user_id: str) -> dict[str, Any]:
        """Counts, so a UI can badge without loading everything."""
        rows = self.inbox(user_id=user_id, unread_only=True, limit=500)
        return {
            "unread": len(rows),
            "action_required": sum(1 for n in rows if n.action_required),
            "critical": sum(1 for n in rows if n.severity == Severity.CRITICAL.value),
        }

    # ==================================================================
    # Preferences
    # ==================================================================
    def set_preference(
        self,
        *,
        user_id: str,
        category: str,
        channel: str = models.NotificationPreference.CHANNEL_IN_APP,
        enabled: bool = True,
        min_severity: str = "INFO",
        quiet_from_hour: Optional[int] = None,
        quiet_to_hour: Optional[int] = None,
    ) -> models.NotificationPreference:
        for hour in (quiet_from_hour, quiet_to_hour):
            if hour is not None and not 0 <= hour <= 23:
                raise ValueError("quiet hours must be 0-23")
        if quiet_from_hour is not None and quiet_to_hour is None:
            raise ValueError("quiet_from_hour without quiet_to_hour would silence everything")
        if quiet_to_hour is not None and quiet_from_hour is None:
            raise ValueError("quiet_to_hour without quiet_from_hour would silence everything")

        preference = self.db.execute(
            select(models.NotificationPreference).where(
                models.NotificationPreference.org_id == self.org_id,
                models.NotificationPreference.user_id == user_id,
                models.NotificationPreference.category == category,
                models.NotificationPreference.channel == channel,
            )
        ).scalars().first()

        if preference is None:
            preference = models.NotificationPreference(
                id=str(uuid.uuid4()), org_id=self.org_id, user_id=user_id,
                category=category, channel=channel, created_at=_now(),
            )
            self.db.add(preference)

        preference.enabled = enabled
        preference.min_severity = min_severity
        preference.quiet_from_hour = quiet_from_hour
        preference.quiet_to_hour = quiet_to_hour
        preference.updated_at = _now()
        self.db.flush()
        return preference


class _Tolerant(dict):
    def __missing__(self, key: str) -> str:  # noqa: D105
        return ""
