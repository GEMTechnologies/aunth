"""Phase 10: notification delivery to a person.

Until this existed, every event the platform emitted was published to a Redis stream that
**nothing consumed for a human**. `report.overdue` - the alert with the clearest financial
consequence - went nowhere. The relay was working perfectly and nobody was told anything.

The design in one line: **events become notifications, notifications are suppressed by
identity rather than by event, and delivery is recorded as evidence.**

Three things it deliberately does not do:

* it does not deliver **outside the platform**. An email notification is an external
  action in the organisation's name, and the Phase 7b outbound path already gates exactly
  that - one gate, one place to audit;
* it does not **resurrect** a dismissed or actioned notification. A recurrence is a new
  decision and should be visible as one;
* it does not raise anything for **bookkeeping events**. Most of what the platform emits is
  a package being frozen or an application being authorised, and notifying on each would
  bury the ones that matter.
"""

from agent.notifications.contract import (  # noqa: F401
    Category,
    DeliveryOutcome,
    NotificationChannel,
    NotificationChannelError,
    RaiseResult,
    Route,
    Severity,
    route_for,
)
from agent.notifications.service import NotificationService  # noqa: F401
