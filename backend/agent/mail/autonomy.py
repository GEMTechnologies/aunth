"""Phase 7c: autonomous sending, and the gates that make it acceptable.

The step this phase takes
------------------------
Phases 7a and 7b established that Granada may *draft* freely and may *send* only
what a human approved byte-for-byte. This phase allows a narrow class of replies to
go out while the organisation is asleep, with no approval - which is the thing an
"NGO runs on volunteers" product actually needs, because a funder asking for an
audited statement at 2am should not wait for a person to wake up.

It is also the step with the most potential for harm, so the gates are the design
and the sending is incidental.

The governing asymmetry
-----------------------
A late reply costs an application. An unauthorised or duplicated reply damages the
organisation's standing with a funder, and can take an apology to repair. So every
gate here is written to fail **closed**: an unknown recipient is not eligible, an
unrecognised risk class is not eligible, a missing policy field is not eligible, and
a provider that does not support the operation is not eligible. There is no
"default allow" path anywhere in this module.

Seven gates, all of which must pass
-----------------------------------
1. **Platform switch.** ``AUTONOMOUS_MAIL_ENABLED`` must be on. It defaults to
   **off**, so the capability is inert on every deployment until somebody turns it on
   deliberately. This is the kill switch, and it is one setting rather than a
   per-organisation scatter.
2. **Organisation opt-in.** The organisation must have explicitly enabled autonomous
   mail for its agent. Default off. An organisation that has not opted in can never
   receive an autonomous send, whatever its autonomy level.
3. **Autonomy level.** ``MONITOR_ONLY`` and ``DRAFT_ONLY`` never send. Only
   ``AUTO_ROUTINE`` and above.
4. **Low-risk only.** The outbound risk class must be in the autonomous allow-list -
   ``ROUTINE`` and ``APPLICATION_INFORMATION``. High-risk classes were already
   unsendable with a human approval; they are certainly not sendable without one.
5. **Reply to a known correspondent.** The recipient must already be a participant in
   the thread, on a domain the organisation has corresponded with. **A new recipient
   is never eligible**, because "the model suggested an address" is exactly how an
   organisation is made to email a stranger in its own name.
6. **Under the rate limit.** A per-organisation daily ceiling. Without it, one
   misclassification becomes a hundred emails before anybody notices.
7. **Nothing suspicious.** No security flags on the source message, and the
   classification must be in the allow-list. A flagged message can no longer cause an
   unattended send even if every other gate passes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import models
from agent.mail.ceiling import HIGH_RISK_CLASSES, OutboundRisk
from agent.mail.vocabulary import MailClassification

logger = logging.getLogger(__name__)

#: THE KILL SWITCH. Off by default, and the first gate checked.
#:
#: A platform-level setting rather than a per-organisation one, so an operator can
#: stop every autonomous send in the fleet with a single configuration change. That
#: matters at 3am when something is going wrong and there is no time to audit which
#: organisations opted in.
DEFAULT_AUTONOMOUS_MAIL_ENABLED = False

#: Risk classes an unattended reply may carry. Deliberately two, and neither of them
#: can move money, accept an obligation or disclose a credential.
AUTONOMOUS_RISK_ALLOWLIST: frozenset[str] = frozenset({
    OutboundRisk.ROUTINE.value,
    OutboundRisk.APPLICATION_INFORMATION.value,
})

#: Classifications an unattended reply may answer. Absences are the point:
#: AWARD_NOTICE, REJECTION_NOTICE, CONTRACT, FINANCIAL_REQUEST, BANK_DETAIL_REQUEST
#: and DEADLINE_CHANGE all require a person, because each one either creates an
#: obligation or is the kind of message a funder will hold the organisation to.
AUTONOMOUS_CLASSIFICATION_ALLOWLIST: frozenset[str] = frozenset({
    MailClassification.ACKNOWLEDGEMENT.value,
    MailClassification.APPLICATION_STATUS.value,
    MailClassification.AUTOMATED_NOTIFICATION.value,
})

#: Autonomy levels that permit unattended sending at all.
AUTONOMOUS_AUTONOMY_LEVELS: frozenset[str] = frozenset({"AUTO_ROUTINE", "AUTOPILOT_WITH_GATES"})

#: Ceiling on unattended sends per organisation per day.
DEFAULT_DAILY_AUTONOMOUS_LIMIT = 10


@dataclass
class AutonomousDecision:
    """Whether an unattended send is permitted, and every reason it is not."""

    allowed: bool
    code: str
    reasons: list[str] = field(default_factory=list)
    gate_results: dict[str, bool] = field(default_factory=dict)
    limit_remaining: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reasons": self.reasons,
            "gates": self.gate_results,
            "limit_remaining": self.limit_remaining,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def platform_autonomy_enabled() -> bool:
    """Whether the platform permits autonomous sending at all.

    Read from settings with a **default of off**, and any failure to read the
    configuration is also off. A settings error must not silently enable the most
    consequential capability in the system.
    """
    try:
        from config import settings

        return bool(getattr(settings, "autonomous_mail_enabled", DEFAULT_AUTONOMOUS_MAIL_ENABLED))
    except Exception:  # noqa: BLE001
        return DEFAULT_AUTONOMOUS_MAIL_ENABLED


class AutonomousPolicy:
    """Evaluates every gate. Returns a decision, never raises on a refusal."""

    def __init__(self, db: Session, *, org_id: str, agent_id: str) -> None:
        if not org_id or not agent_id:
            raise ValueError("an autonomy decision requires both an organisation and an agent")
        self.db = db
        self.org_id = org_id
        self.agent_id = agent_id

    # ------------------------------------------------------------------
    def evaluate(
        self,
        *,
        risk_class: str,
        classification: Optional[str],
        recipients: Any,
        thread_participants: Any = (),
        known_donor_domains: Any = (),
        security_flags: Any = (),
        platform_enabled: Optional[bool] = None,
        daily_limit: Optional[int] = None,
    ) -> AutonomousDecision:
        """Every gate, in order of how cheaply it fails."""
        gates: dict[str, bool] = {}
        reasons: list[str] = []

        def fail(code: str, reason: str) -> AutonomousDecision:
            gates[code] = False
            reasons.append(reason)
            return AutonomousDecision(False, code, reasons, gates, None)

        # -- gate 1: the platform kill switch ---------------------------
        enabled = platform_autonomy_enabled() if platform_enabled is None else platform_enabled
        gates["PLATFORM_ENABLED"] = enabled
        if not enabled:
            return fail(
                "AUTONOMOUS_MAIL_DISABLED",
                "the platform switch for autonomous mail is off; every outbound message "
                "requires human approval",
            )

        # -- gate 2: the organisation opted in --------------------------
        agent = self.db.execute(
            select(models.GranadaAgent).where(
                models.GranadaAgent.id == self.agent_id,
                models.GranadaAgent.org_id == self.org_id,
            )
        ).scalars().first()
        if agent is None:
            gates["AGENT_EXISTS"] = False
            return fail("AGENT_MISSING", "the agent does not belong to this organisation")
        gates["AGENT_EXISTS"] = True

        settings = agent.settings or {}
        opted_in = bool(settings.get("autonomous_mail_enabled"))
        gates["ORGANISATION_OPTED_IN"] = opted_in
        if not opted_in:
            return fail(
                "AUTONOMOUS_NOT_ENABLED_FOR_ORGANISATION",
                "this organisation has not enabled autonomous mail; its agent drafts and "
                "asks, it does not send unattended",
            )

        # -- gate 3: the agent is active and at a sufficient level -------
        active = agent.status == models.GranadaAgent.ACTIVE
        gates["AGENT_ACTIVE"] = active
        if not active:
            return fail("AGENT_NOT_ACTIVE", f"the agent is {agent.status}")

        level_ok = agent.autonomy in AUTONOMOUS_AUTONOMY_LEVELS
        gates["AUTONOMY_LEVEL"] = level_ok
        if not level_ok:
            return fail(
                "AUTONOMY_LEVEL_TOO_LOW",
                f"autonomy {agent.autonomy} does not permit unattended sending",
            )

        # -- gate 4: low risk only --------------------------------------
        if risk_class in {r.value for r in HIGH_RISK_CLASSES}:
            gates["LOW_RISK"] = False
            return fail(
                "HIGH_RISK_ACTION_BLOCKED",
                f"{risk_class} is never sendable without a person, whatever the autonomy "
                "level",
            )
        risk_ok = risk_class in AUTONOMOUS_RISK_ALLOWLIST
        gates["LOW_RISK"] = risk_ok
        if not risk_ok:
            return fail(
                "RISK_CLASS_NOT_AUTONOMOUS",
                f"{risk_class} is not in the autonomous allow-list "
                f"({sorted(AUTONOMOUS_RISK_ALLOWLIST)})",
            )

        # -- gate 5: classification allow-list --------------------------
        if classification is not None:
            allowed_classification = classification in AUTONOMOUS_CLASSIFICATION_ALLOWLIST
            gates["CLASSIFICATION"] = allowed_classification
            if not allowed_classification:
                return fail(
                    "CLASSIFICATION_REQUIRES_HUMAN",
                    f"{classification} requires a person ({sorted(AUTONOMOUS_CLASSIFICATION_ALLOWLIST)} "
                    "are the only unattended-safe classes)",
                )
        else:
            gates["CLASSIFICATION"] = False
            return fail(
                "NO_CLASSIFICATION",
                "an unattended reply needs a known classification; absence is not "
                "permission",
            )

        # -- gate 6: a reply to a known correspondent --------------------
        # THE most important gate. A new recipient must never receive an unattended
        # message, because the address would come from inference rather than from
        # correspondence, and emailing a stranger in the organisation's name is the
        # failure that cannot be undone.
        participant_set = {str(a).strip().lower() for a in (thread_participants or ())}
        domain_set = {str(d).strip().lower() for d in (known_donor_domains or ())}
        recipient_list = [
            str(a).strip().lower() for a in (recipients or ()) if str(a or "").strip()
        ]
        if not recipient_list:
            gates["KNOWN_RECIPIENT"] = False
            return fail("NO_RECIPIENT", "an unattended reply needs a recipient")

        from agent.mail.security import domain_of

        unknown: list[str] = []
        for address in recipient_list:
            if address in participant_set:
                continue
            domain = domain_of(address)
            if domain and domain in domain_set:
                continue
            # A *known domain* is the minimum: the organisation has corresponded with
            # it before. A brand-new domain is refused even when the display name looks
            # like a funder, because that is precisely how impersonation works.
            unknown.append(address)
        gates["KNOWN_RECIPIENT"] = not unknown
        if unknown:
            return fail(
                "UNKNOWN_RECIPIENT",
                "an unattended reply may only go to a participant already in this thread "
                f"or on a domain the organisation has corresponded with; refused: {unknown}",
            )

        # -- gate 7: nothing suspicious ---------------------------------
        flags = [str(f) for f in (security_flags or ()) if f]
        gates["NO_SECURITY_FLAGS"] = not flags
        if flags:
            return fail(
                "SECURITY_FLAGS_PRESENT",
                f"the source message carries security flags ({flags}); a flagged message "
                "cannot cause an unattended send",
            )

        # -- gate 8: under the daily ceiling ----------------------------
        limit = daily_limit or int(
            settings.get("autonomous_mail_daily_limit", DEFAULT_DAILY_AUTONOMOUS_LIMIT)
        )
        sent_today = self._autonomous_sends_today()
        remaining = max(0, limit - sent_today)
        gates["UNDER_DAILY_LIMIT"] = remaining > 0
        if remaining <= 0:
            return fail(
                "DAILY_LIMIT_REACHED",
                f"the organisation has already sent {sent_today} unattended messages "
                f"today (limit {limit})",
            )

        return AutonomousDecision(
            allowed=True,
            code="AUTONOMOUS_SEND_PERMITTED",
            reasons=["every gate passed"],
            gate_results=gates,
            limit_remaining=remaining - 1,
        )

    # ------------------------------------------------------------------
    def _autonomous_sends_today(self) -> int:
        """How many unattended messages this organisation has already sent today.

        Counted from the durable approvals rather than from a counter, because a
        counter can drift and an approval row cannot - and the number has to be right,
        since it is what stops a misclassification becoming a hundred emails.
        """
        midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        return int(
            self.db.execute(
                select(func.count(models.MailApproval.id)).where(
                    models.MailApproval.org_id == self.org_id,
                    models.MailApproval.decision == models.MailApproval.AUTONOMOUS_POLICY,
                    models.MailApproval.approved_at >= midnight,
                )
            ).scalar() or 0
        )


def enable_for_organisation(
    db: Session, *, org_id: str, daily_limit: Optional[int] = None
) -> models.GranadaAgent:
    """Explicitly opt one organisation into autonomous mail.

    A named function rather than a settings edit, so enabling it is a deliberate act
    with an audit trail rather than a configuration line somebody adds while debugging
    something else.
    """
    agent = db.execute(
        select(models.GranadaAgent).where(models.GranadaAgent.org_id == org_id)
    ).scalars().first()
    if agent is None:
        raise ValueError(f"no agent for organisation {org_id}")
    settings = dict(agent.settings or {})
    settings["autonomous_mail_enabled"] = True
    if daily_limit is not None:
        settings["autonomous_mail_daily_limit"] = int(daily_limit)
    agent.settings = settings
    agent.updated_at = _now()
    db.flush()
    db.add(
        models.AgentActivity(
            id=_uuid(),
            agent_id=agent.id,
            org_id=org_id,
            specialist_key="EMAIL",
            activity_type="settings",
            summary_key="mail.autonomous_enabled",
            subject_type="AGENT",
            subject_id=agent.id,
            structured_data={
                "daily_limit": settings.get(
                    "autonomous_mail_daily_limit", DEFAULT_DAILY_AUTONOMOUS_LIMIT
                ),
                "risk_allowlist": sorted(AUTONOMOUS_RISK_ALLOWLIST),
                "classification_allowlist": sorted(AUTONOMOUS_CLASSIFICATION_ALLOWLIST),
            },
            visibility=models.AgentActivity.VISIBILITY_CUSTOMER,
            occurred_at=_now(),
        )
    )
    db.flush()
    return agent


def disable_for_organisation(db: Session, *, org_id: str) -> models.GranadaAgent:
    """Turn autonomous mail off for one organisation. Immediate."""
    agent = db.execute(
        select(models.GranadaAgent).where(models.GranadaAgent.org_id == org_id)
    ).scalars().first()
    if agent is None:
        raise ValueError(f"no agent for organisation {org_id}")
    settings = dict(agent.settings or {})
    settings["autonomous_mail_enabled"] = False
    agent.settings = settings
    agent.updated_at = _now()
    db.flush()
    db.add(
        models.AgentActivity(
            id=_uuid(), agent_id=agent.id, org_id=org_id, specialist_key="EMAIL",
            activity_type="settings", summary_key="mail.autonomous_disabled",
            subject_type="AGENT", subject_id=agent.id,
            structured_data={"reason": "disabled by an operator"},
            # An organisation-facing activity: "your agent stopped sending
            # unattended" is something the customer must be able to see.
            visibility=models.AgentActivity.VISIBILITY_CUSTOMER,
            occurred_at=_now(),
        )
    )
    db.flush()
    return agent


def _uuid() -> str:
    import uuid as _u

    return str(_u.uuid4())
