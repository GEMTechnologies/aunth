"""Notification channels. `INTERNAL` is the boundary that matters."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from agent.notifications.contract import DeliveryOutcome, NotificationChannelError


@dataclass
class RecordedNotification:
    """One thing a channel was asked to deliver."""

    notification_id: str
    recipient: str
    title: str
    body: str
    severity: str
    category: str
    action_required: bool
    delivered_at: datetime


class InAppChannel:
    """The default channel. Writes inside the platform and never leaves it.

    It performs no external action, which is why it needs no gate: the notification row is
    already committed by the time this is called, and this records that somebody was shown
    it. A channel whose delivery fails leaves the notification intact and simply has no
    delivery record - which is the correct state, because the person can still see it.
    """

    name = "IN_APP"
    internal = True

    def __init__(self) -> None:
        self.delivered: list[RecordedNotification] = []
        self._lock = threading.Lock()

    @property
    def delivery_count(self) -> int:
        """THE number the suppression tests assert."""
        return len(self.delivered)

    def deliver(self, *, notification: Any, recipient: str) -> DeliveryOutcome:
        with self._lock:
            self.delivered.append(
                RecordedNotification(
                    notification_id=getattr(notification, "id", ""),
                    recipient=recipient,
                    title=getattr(notification, "title", ""),
                    body=getattr(notification, "body", "") or "",
                    severity=getattr(notification, "severity", ""),
                    category=getattr(notification, "category", ""),
                    action_required=bool(getattr(notification, "action_required", False)),
                    delivered_at=datetime.now(timezone.utc),
                )
            )
        return DeliveryOutcome(channel=self.name, result="DELIVERED")


class ExternalChannel:
    """A channel that leaves the platform. **Refused unless routed through the gate.**

    Deliberately unable to deliver on its own. An email notification is an external action
    - it reaches a person outside the platform, in the organisation's name - and the
    Phase 7b outbound path already exists to gate exactly that. A second egress would be a
    second place to audit, and the one that gets forgotten is the one that sends.
    """

    name = "EXTERNAL"
    internal = False

    def __init__(self, name: str = "EXTERNAL") -> None:
        self.name = name
        self.attempts: list[str] = []

    def deliver(self, *, notification: Any, recipient: str) -> DeliveryOutcome:
        self.attempts.append(getattr(notification, "id", ""))
        raise NotificationChannelError(
            f"{self.name} leaves the platform. An external notification must go through "
            "the gated outbound path (agent.mail.send_service), not through a second "
            "egress - one gate, one place to audit."
        )


class FailingChannel:
    """A channel that always raises, so the failure path is exercised."""

    name = "FAILING"
    internal = True

    def __init__(self, error_code: str = "CHANNEL_UNAVAILABLE") -> None:
        self.error_code = error_code
        self.attempts = 0

    def deliver(self, *, notification: Any, recipient: str) -> DeliveryOutcome:
        self.attempts += 1
        raise NotificationChannelError(self.error_code)
