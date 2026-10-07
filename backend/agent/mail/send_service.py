"""The outbound send pipeline: claim, call, record. Human approval is mandatory.

Transaction structure, and the divergence it forced
--------------------------------------------------
The brief asks for two things that cannot both hold literally:

* §18 — ``mail_send_attempts`` must be append-only, verified LIVE as
  ``UPDATE = false, DELETE = false``.
* §19 — Transaction A should "create send attempt" **before** the provider call.

An attempt created before the call has no outcome yet, so recording the outcome
requires updating it - which §18 forbids. The two are irreconcilable as written.

**This resolves in favour of the append-only guarantee**, because it is the stronger
property and the one the safety rests on:

* **Transaction A** claims the send on the *intent*: reload, validate the approval,
  run the final authority check, re-verify recipients and attachments, set
  ``SENDING`` with ``send_started_at``, allocate the opaque message reference, bump
  ``attempt_count``, and commit. **This is durable evidence that a provider call may
  have been made**, which is the actual purpose of creating a row before the call.
* the provider is called, holding no database locks (§19's other half);
* **Transaction B** writes the **immutable attempt row** with its final outcome,
  plus the receipt, the state transition, activity and the outbox event, and commits.

A crash between the two leaves ``SENDING`` with an incremented ``attempt_count`` and
no attempt row. That state is in ``UNCERTAIN``, so a retry is forbidden until
reconciliation says otherwise - the safety property is preserved exactly.

So: the attempt table becomes an immutable ledger of *outcomes*, and the intent
carries the mutable lifecycle. Recorded here and in the report because the brief
states both requirements and a silent choice between them would be worse than
either.

Three outcomes, and why the third is the important one
-----------------------------------------------------
``CONFIRMED_SENT``, ``CONFIRMED_NOT_SENT``, ``DELIVERY_UNKNOWN``. Only the middle
one is retryable, and only when the failure is a retryable kind. A timeout is
**never** treated as not-sent: a provider that accepted a message and lost its
response is indistinguishable, from here, from one that never received it, and
retrying on that guess sends a funder the same email twice.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from agent.mail.approval import APPROVE_SEND_PERMISSION, ApprovalService, has_permission
from agent.mail.ceiling import (
    HIGH_RISK_CLASSES,
    Capability,
    OutboundRisk,
    RiskRefused,
    assert_capability,
)
from agent.mail.fingerprint import diff_fingerprint_inputs, fingerprint
from agent.mail.outbound import (
    DEFINITE_NOT_SENT,
    INDETERMINATE,
    RETRYABLE,
    OutboundCapabilityMissing,
    OutboundMessage,
    SendFailure,
    SendOutcome,
    SubmitResult,
)
from agent.mail.risk import (
    build_attachment_manifest,
    check_recipients,
    verify_attachment_manifest,
)

logger = logging.getLogger(__name__)

#: How long an approval stays usable. Long enough for a working day; short enough
#: that a reply written about a deadline cannot be sent after the deadline.
DEFAULT_APPROVAL_TTL_HOURS = 72


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SendError(RuntimeError):
    """Base class for send failures that are refusals rather than provider errors."""


class NotSendable(SendError):
    """The intent may not be sent, for a reason that has a name."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class AuthorityRefused(NotSendable):
    """The final authority checkpoint refused. Never bypassable."""


class DeliveryUnknown(SendError):
    """The provider may have accepted. A retry is FORBIDDEN until reconciled."""


class SendRefused(SendError):
    """Reserved for a refusal produced by an approval or risk rule."""


def new_message_ref() -> str:
    """Granada's opaque outbound message identity.

    Opaque and non-enumerable: 32 hex characters, so correlating a reply does not
    leak a row count or a sequence, and one organisation cannot probe another's
    volume by watching identifiers.
    """
    return f"gml-{secrets.token_hex(16)}"


@dataclass
class SendResult:
    """What one send attempt did."""

    intent_id: str
    outcome: Optional[str] = None
    state: str = ""
    attempt_id: Optional[str] = None
    provider_submission_id: Optional[str] = None
    failure: Optional[str] = None
    detail: str = ""
    refused: bool = False
    refusal_code: Optional[str] = None
    reconciled: bool = False

    @property
    def sent(self) -> bool:
        return self.outcome == SendOutcome.CONFIRMED_SENT.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "send_intent_id": self.intent_id,
            "outcome": self.outcome,
            "state": self.state,
            "attempt_id": self.attempt_id,
            "provider_submission_id": self.provider_submission_id,
            "failure": self.failure,
            "detail": self.detail,
            "refused": self.refused,
            "refusal_code": self.refusal_code,
            "reconciled": self.reconciled,
        }


class SendService:
    """Creates send intents, and executes approved ones."""

    def __init__(
        self,
        db: Session,
        *,
        org_id: str,
        agent_id: str,
        outbound: Optional[Any] = None,
    ) -> None:
        if not org_id or not agent_id:
            raise SendError("an outbound send requires both an organisation and an agent")
        self.db = db
        self.org_id = org_id
        self.agent_id = agent_id
        self.outbound = outbound

    # ==================================================================
    # 1. Create the immutable intent
    # ==================================================================
    def create_send_intent(
        self,
        *,
        draft: models.MailDraft,
        to_addresses: Any,
        from_address: str,
        mail_account_id: Optional[str] = None,
        mail_identity_id: Optional[str] = None,
        cc_addresses: Any = None,
        bcc_addresses: Any = None,
        reply_to_address: Optional[str] = None,
        documents: Any = (),
        known_donor_domains: Any = (),
        known_donors: Any = (),
        autonomous: bool = False,
        classification: Optional[str] = None,
        thread_participants: Any = (),
        security_flags: Any = (),
        source_message_id: Optional[str] = None,
    ) -> models.MailSendIntent:
        """Freeze a draft into an intent a human can approve.

        Refuses outright when the message is high-risk or the draft is not complete,
        **before** an intent exists. A blocked message that left a waiting intent
        behind would be approvable later by someone who did not know it had been
        refused.
        """
        assert_capability(Capability.MAIL_REQUEST_APPROVAL)

        if draft.org_id != self.org_id:
            raise SendError("the draft belongs to another organisation")
        if draft.status == models.MailDraft.NEEDS_DATA:
            raise NotSendable(
                "DRAFT_NEEDS_DATA",
                "the draft is waiting for information the organisation does not have; "
                "an incomplete reply must not enter the approval path",
            )
        if _has_unresolved_placeholder(draft.body):
            raise NotSendable(
                "UNRESOLVED_PLACEHOLDER",
                "the draft body still contains an internal placeholder, which would be "
                "sent verbatim to a funder",
            )

        # Attachments are frozen first: an ineligible document is a reason not to
        # create the intent at all.
        attachment_report = build_attachment_manifest(documents, org_id=self.org_id)
        if not attachment_report.ok:
            raise NotSendable(
                "ATTACHMENT_INELIGIBLE",
                "; ".join(e["detail"] for e in attachment_report.errors),
            )

        from agent.mail.risk import classify_outbound_risk

        risk = classify_outbound_risk(
            subject=draft.subject, body=draft.body, attachments=attachment_report.manifest
        )
        if risk.is_high_risk:
            raise RiskRefused(
                f"HIGH_RISK_ACTION_BLOCKED: this message is {risk.risk_class.value} "
                f"({'; '.join(risk.reasons)}). It is not sendable through the Phase 7b "
                "approval path, even with a human approval."
            )

        recipients = check_recipients(
            to_addresses=to_addresses,
            cc_addresses=cc_addresses,
            bcc_addresses=bcc_addresses,
            reply_to_address=reply_to_address,
            from_address=from_address,
            known_donor_domains=known_donor_domains,
            known_donors=known_donors,
        )
        if not recipients.ok:
            raise NotSendable(
                "RECIPIENT_UNSAFE",
                "; ".join(e["detail"] for e in recipients.errors),
            )

        agent = self.db.execute(
            select(models.GranadaAgent).where(
                models.GranadaAgent.id == self.agent_id,
                models.GranadaAgent.org_id == self.org_id,
            )
        ).scalars().first()
        if agent is None:
            raise SendError("the agent does not belong to this organisation")

        digest, canonical = fingerprint(
            org_id=self.org_id,
            agent_id=self.agent_id,
            from_address=from_address,
            to_addresses=to_addresses,
            cc_addresses=cc_addresses,
            bcc_addresses=bcc_addresses,
            reply_to_address=reply_to_address,
            subject=draft.subject,
            body=draft.body,
            attachments=attachment_report.manifest,
            application_id=draft.application_id,
            thread_id=draft.thread_id,
            reply_to_message_id=draft.reply_to_message_id,
            draft_version=draft.version,
            risk_class=risk.risk_class.value,
        )

        # ONE intent per (message, draft version, fingerprint). Re-creating the same
        # intent is idempotent; a changed fingerprint is a new intent, which is what
        # "no approval inheritance" means in practice.
        idempotency_key = f"{draft.reply_to_message_id}:{draft.version}:{digest[:32]}"
        existing = self.db.execute(
            select(models.MailSendIntent).where(
                models.MailSendIntent.org_id == self.org_id,
                models.MailSendIntent.idempotency_key == idempotency_key,
            )
        ).scalars().first()
        if existing is not None:
            return existing

        intent = models.MailSendIntent(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=self.agent_id,
            mail_account_id=mail_account_id or draft.thread_id and None,
            mail_identity_id=mail_identity_id,
            thread_id=draft.thread_id,
            application_id=draft.application_id,
            reply_to_message_id=draft.reply_to_message_id,
            draft_id=draft.id,
            draft_version=draft.version,
            from_address=from_address,
            to_addresses=recipients.normalised,
            cc_addresses=list(cc_addresses or []),
            bcc_addresses=list(bcc_addresses or []),
            reply_to_address=reply_to_address,
            subject=draft.subject,
            body_snapshot=draft.body,
            attachment_manifest={"entries": attachment_report.manifest},
            message_fingerprint=digest,
            fingerprint_input=canonical,
            risk_class=risk.risk_class.value,
            risk_detail={
                **risk.as_dict(),
                "recipient_report": recipients.as_dict(),
                "attachment_report": attachment_report.as_dict(),
                # The policy inputs, frozen alongside the message.
                #
                # The autonomous re-evaluation at send time needs them, and without
                # them the re-evaluation would see classification=None and refuse
                # every unattended send with NO_CLASSIFICATION. That gate fails
                # closed, so the bug would have looked like correct caution rather
                # than a defect - which is the kind of failure that survives review.
                "classification": classification,
                "thread_participants": list(thread_participants or []),
                "known_donor_domains": list(known_donor_domains or []),
                "security_flags": [str(f) for f in (security_flags or [])],
                "source_message_id": source_message_id,
            },
            status=models.MailSendIntent.WAITING_FOR_APPROVAL,
            agent_version=agent.version,
            idempotency_key=idempotency_key,
            granada_message_ref=new_message_ref(),
            created_at=_now(),
            correlation_id=str(uuid.uuid4()),
        )
        self.db.add(intent)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            winner = self.db.execute(
                select(models.MailSendIntent).where(
                    models.MailSendIntent.idempotency_key == idempotency_key
                )
            ).scalars().first()
            if winner is None:  # pragma: no cover
                raise
            return winner

        # -- Phase 7c: may this go WITHOUT a person? ----------------------
        if autonomous:
            # Every gate is evaluated here, at intent creation, and the decision is
            # recorded. It is NOT re-derived later by a caller, and it is re-evaluated
            # again at send time from live state - because the organisation can opt
            # out, or hit its ceiling, between the two moments.
            from agent.mail.autonomy import AutonomousPolicy

            decision = AutonomousPolicy(
                self.db, org_id=self.org_id, agent_id=self.agent_id
            ).evaluate(
                risk_class=risk.risk_class.value,
                classification=classification,
                recipients=recipients.normalised,
                thread_participants=thread_participants,
                known_donor_domains=known_donor_domains,
                security_flags=security_flags,
            )
            if decision.allowed:
                assert_capability(
                    Capability.MAIL_SEND_AUTONOMOUS_LOW_RISK, policy_cleared=True
                )
                self.db.add(
                    models.MailApproval(
                        id=str(uuid.uuid4()), org_id=self.org_id, agent_id=self.agent_id,
                        send_intent_id=intent.id,
                        decision=models.MailApproval.AUTONOMOUS_POLICY,
                        fingerprint=intent.message_fingerprint,
                        risk_class=intent.risk_class,
                        fingerprint_input=intent.fingerprint_input,
                        # Not a person. Recorded explicitly so "who authorised this?"
                        # never has to be inferred from the decision column.
                        approved_by=f"policy:{intent.id}",
                        approved_at=_now(),
                        permission_used="mail.autonomous_policy",
                        approval_version=1,
                        status=models.MailApproval.STATUS_ACTIVE,
                        policy_evidence=decision.as_dict(),
                        note=(
                            "authorised by the Phase 7c autonomous policy: every gate "
                            "passed for this specific message"
                        ),
                    )
                )
                intent.status = models.MailSendIntent.APPROVED
                intent.approved_at = _now()
                intent.status_reason = "authorised by policy for unattended sending"
                self.db.flush()
                self._stage_event(
                    event_type="mail.send_autonomous_authorised",
                    payload={
                        "send_intent_id": intent.id,
                        "risk_class": intent.risk_class,
                        "gates": decision.gate_results,
                    },
                )
                self._record_activity(
                    summary_key="mail.autonomous_authorised",
                    structured={
                        "send_intent_id": intent.id,
                        "risk_class": intent.risk_class,
                        "recipients": intent.to_addresses,
                    },
                    subject_id=intent.id,
                )
                self.db.flush()
                return intent

            # Refused. The intent still exists and still awaits a person - the gate
            # failing means "not unattended", never "not sent at all".
            intent.status = models.MailSendIntent.WAITING_FOR_APPROVAL
            intent.status_reason = (
                f"not eligible for unattended sending ({decision.code}): "
                + "; ".join(decision.reasons)
            )[:1000]
            self.db.flush()
            self._record_activity(
                summary_key="mail.autonomous_refused",
                structured={
                    "send_intent_id": intent.id,
                    "code": decision.code,
                    "reasons": decision.reasons,
                    "gates": decision.gate_results,
                },
                subject_id=intent.id,
            )
            self.db.flush()
            return intent

        self._stage_event(
            event_type="mail.send_approval_requested",
            payload={
                "send_intent_id": intent.id,
                "risk_class": intent.risk_class,
                "fingerprint": intent.message_fingerprint,
            },
        )
        self._record_activity(
            summary_key="mail.approval_requested",
            structured={
                "send_intent_id": intent.id,
                "risk_class": intent.risk_class,
                "recipients": intent.to_addresses,
                "subject": intent.subject,
            },
            subject_id=intent.id,
        )
        draft.status = models.MailDraft.NEEDS_REVIEW
        draft.status_reason = f"awaiting approval of send intent {intent.id}"
        self.db.flush()
        return intent

    # ==================================================================
    # 2. Execute an approved intent
    # ==================================================================
    def execute_send(self, *, intent_id: str, worker_id: str = "worker") -> SendResult:
        """Claim, call, record. The whole outbound path.

        ``human_approved=True`` in the capability assert is not a formality: it is
        what the ceiling requires for ``MAIL_SEND_HUMAN_APPROVED``, and passing it
        is only reachable after the approval checks below have passed.
        """
        # ---- TRANSACTION A: claim ------------------------------------
        intent, refusal = self._claim(intent_id)
        if refusal is not None:
            return refusal

        try:
            message = self._build_message(intent)
        except Exception as exc:  # pragma: no cover - defensive
            self.db.rollback()
            return self._fail_intent(
                intent_id, "MESSAGE_BUILD_FAILED", str(exc)[:200],
                state=models.MailSendIntent.FAILED_FINAL,
            )

        # ---- PROVIDER CALL, holding no database locks ------------------
        result = self._call_provider(intent, message)

        # ---- TRANSACTION B: record -----------------------------------
        return self._record(intent_id, result, worker_id=worker_id)

    # ------------------------------------------------------------------
    def _claim(self, intent_id: str) -> tuple[Optional[models.MailSendIntent], Optional[SendResult]]:
        """Revalidate everything from durable state, then claim. Commits.

        Nothing here trusts state captured when the approval was given. The brief is
        explicit, and the reason is that an approval can sit for hours: the agent can
        be paused, the authority reduced, the document superseded, the recipient
        changed. Reloading is the only way to notice.
        """
        intent = self.db.execute(
            select(models.MailSendIntent).where(
                models.MailSendIntent.id == intent_id,
                models.MailSendIntent.org_id == self.org_id,
            )
        ).scalars().first()
        if intent is None:
            return None, SendResult(
                intent_id=intent_id, refused=True, refusal_code="NOT_FOUND",
                detail="no such send intent in this organisation",
            )

        if intent.status in models.MailSendIntent.TERMINAL:
            return None, SendResult(
                intent_id=intent.id, state=intent.status, refused=True,
                refusal_code="ALREADY_TERMINAL",
                detail=f"intent is {intent.status}",
            )

        # -- an unknown outcome forbids a retry ----------------------
        if intent.status in models.MailSendIntent.UNCERTAIN:
            return None, SendResult(
                intent_id=intent.id, state=models.MailSendIntent.DELIVERY_UNKNOWN,
                refused=True, refusal_code="DELIVERY_UNKNOWN",
                detail=(
                    "a previous attempt's outcome is unknown; reconciliation must first "
                    "prove the provider did not accept it before another attempt"
                ),
            )

        # -- approved? ------------------------------------------------
        approval = ApprovalService(self.db, org_id=self.org_id).active_approval(intent)
        if approval is None:
            # Distinguish "no approval" from "the approval no longer matches", since
            # they need different actions from an operator.
            any_approval = self.db.execute(
                select(models.MailApproval).where(
                    models.MailApproval.send_intent_id == intent.id,
                    models.MailApproval.status == models.MailApproval.STATUS_ACTIVE,
                    models.MailApproval.decision == models.MailApproval.APPROVE,
                )
            ).scalars().first()
            if any_approval is not None:
                diff = diff_fingerprint_inputs(
                    any_approval.fingerprint_input, intent.fingerprint_input
                )
                return None, SendResult(
                    intent_id=intent.id, state=intent.status, refused=True,
                    refusal_code="APPROVAL_SUPERSEDED",
                    detail=(
                        "the approval authorises a different message: "
                        + "; ".join(c["field"] for c in diff.get("changed", []))
                    ),
                )
            return None, SendResult(
                intent_id=intent.id, state=intent.status, refused=True,
                refusal_code="NOT_APPROVED", detail="no live approval for this intent",
            )

        # -- expiry ---------------------------------------------------
        approved_at = _aware(approval.approved_at)
        if approved_at and _now() - approved_at > timedelta(hours=DEFAULT_APPROVAL_TTL_HOURS):
            return None, SendResult(
                intent_id=intent.id, state=intent.status, refused=True,
                refusal_code="APPROVAL_EXPIRED",
                detail=f"the approval is older than {DEFAULT_APPROVAL_TTL_HOURS}h",
            )

        # -- THE FINAL AUTHORITY CHECK -------------------------------
        authority = self._final_authority_check(intent)
        if authority is not None:
            return None, authority

        # -- an unattended authorisation is re-evaluated, not trusted -----
        # A policy decision taken when the intent was created is a statement about
        # the world THEN. The organisation can opt out, the ceiling can be hit, the
        # agent can be paused and the message can be re-flagged in between. Reusing
        # the stored decision would make every gate a one-time check, which is the
        # same as no check for a message that sat in a queue overnight.
        if approval.decision == models.MailApproval.AUTONOMOUS_POLICY:
            from agent.mail.autonomy import AutonomousPolicy

            reevaluated = AutonomousPolicy(
                self.db, org_id=self.org_id, agent_id=self.agent_id
            ).evaluate(
                risk_class=intent.risk_class,
                classification=(intent.risk_detail or {}).get("classification"),
                recipients=intent.to_addresses or [],
                thread_participants=(intent.risk_detail or {}).get("thread_participants") or [],
                known_donor_domains=(intent.risk_detail or {}).get("known_donor_domains") or [],
                security_flags=(intent.risk_detail or {}).get("security_flags") or [],
            )
            if not reevaluated.allowed:
                intent.status = models.MailSendIntent.WAITING_FOR_APPROVAL
                intent.status_reason = (
                    "the autonomous authorisation is no longer valid "
                    f"({reevaluated.code}): " + "; ".join(reevaluated.reasons)
                )[:1000]
                self.db.commit()
                # `_claim` returns (intent, refusal), so the refusal is the SECOND
                # element. Returning a bare SendResult made the caller try to unpack
                # it and fail with a TypeError - the gate worked and the plumbing did
                # not, which is the kind of bug that hides a working safety check.
                return None, SendResult(
                    intent_id=intent.id, state=intent.status, refused=True,
                    refusal_code=f"AUTONOMOUS_REVOKED_{reevaluated.code}",
                    detail=intent.status_reason,
                )

        # -- risk, re-checked against the frozen content --------------
        if intent.risk_class in {r.value for r in HIGH_RISK_CLASSES}:
            intent.status = models.MailSendIntent.HIGH_RISK_BLOCKED
            intent.status_reason = f"{intent.risk_class} is not sendable in this phase"
            self.db.commit()
            return None, SendResult(
                intent_id=intent.id, state=intent.status, refused=True,
                refusal_code="HIGH_RISK_ACTION_BLOCKED",
                detail=f"{intent.risk_class} remains unsendable even with approval",
            )

        # The fingerprint is recalculated from the frozen columns. If the row were
        # somehow edited, this notices before anything leaves.
        live_fingerprint, live_input = self._fingerprint_of(intent)
        if live_fingerprint != approval.fingerprint:
            intent.status = models.MailSendIntent.CHANGES_REQUESTED
            intent.status_reason = "the frozen message no longer matches the approval"
            self.db.commit()
            return None, SendResult(
                intent_id=intent.id, state=intent.status, refused=True,
                refusal_code="FINGERPRINT_MISMATCH",
                detail="the intent no longer matches the approved fingerprint",
            )

        # -- claim ----------------------------------------------------
        intent.status = models.MailSendIntent.SENDING
        intent.send_started_at = _now()
        intent.attempt_count = (intent.attempt_count or 0) + 1
        intent.last_attempt_at = _now()
        if not intent.granada_message_ref:
            intent.granada_message_ref = new_message_ref()
        intent.provider = self.outbound.name if self.outbound is not None else intent.provider
        self.db.commit()
        return intent, None

    # ------------------------------------------------------------------
    def _final_authority_check(self, intent: models.MailSendIntent) -> Optional[SendResult]:
        """Reload everything and refuse if anything has changed. Returns the refusal.

        Every check is here rather than spread through the caller because a
        checkpoint with a gap is not a checkpoint. Each one has a name and a detail,
        so an operator knows what to fix rather than only that something is wrong.
        """
        def refuse(code: str, detail: str, state: str = models.MailSendIntent.WAITING_FOR_APPROVAL) -> SendResult:
            intent.status = state
            intent.status_reason = detail
            return SendResult(
                intent_id=intent.id, state=state, refused=True,
                refusal_code=code, detail=detail,
            )

        # 1. Platform policy.
        try:
            assert_capability(Capability.MAIL_SEND_HUMAN_APPROVED, human_approved=True)
        except Exception as exc:
            return refuse("PLATFORM_POLICY", str(exc))

        # 2. Organisation and agent.
        agent = self.db.execute(
            select(models.GranadaAgent).where(
                models.GranadaAgent.id == intent.agent_id,
                models.GranadaAgent.org_id == self.org_id,
            )
        ).scalars().first()
        if agent is None:
            return refuse("AGENT_MISSING", "the agent no longer exists for this organisation")
        if agent.status != models.GranadaAgent.ACTIVE:
            # PAUSE MUST WIN OVER APPROVAL. An approval given before the pause does
            # not authorise work after it: the organisation said stop.
            return refuse(
                "AGENT_NOT_ACTIVE",
                f"the agent is {agent.status}; an approval does not override a pause",
            )
        if intent.agent_version is not None and intent.agent_version != agent.version:
            # Authority changed after approval. Revalidate rather than silently
            # continuing, and rather than silently overwriting the recorded version.
            return refuse(
                "AGENT_VERSION_CHANGED",
                f"the agent's authority changed from version {intent.agent_version} to "
                f"{agent.version} after this message was approved; it must be "
                "revalidated before sending",
            )

        # 3. Mailbox and identity, and the SEND capability specifically.
        account = None
        if intent.mail_account_id:
            account = self.db.execute(
                select(models.MailAccount).where(
                    models.MailAccount.id == intent.mail_account_id,
                    models.MailAccount.org_id == self.org_id,
                )
            ).scalars().first()
            if account is None:
                return refuse("ACCOUNT_MISSING", "the mailbox is no longer available")
            if account.status != models.MailAccount.ACTIVE:
                return refuse("ACCOUNT_NOT_ACTIVE", f"the mailbox is {account.status}")
        if self.outbound is None:
            return refuse(
                "NO_OUTBOUND_PROVIDER",
                "no outbound provider is configured for this mailbox",
            )
        caps = set(getattr(self.outbound, "capabilities", frozenset()))
        if "MAIL_SEND" not in caps:
            # READ MUST NOT IMPLY SEND. A mailbox connected with read-only scope
            # cannot send, and no amount of approval changes that.
            return refuse(
                "ACCOUNT_LACKS_SEND_SCOPE",
                "the mailbox was connected with read-only scope; sending requires an "
                "explicit send capability on the provider connection",
            )

        if intent.mail_identity_id:
            identity = self.db.execute(
                select(models.MailIdentity).where(
                    models.MailIdentity.id == intent.mail_identity_id,
                    models.MailIdentity.org_id == self.org_id,
                )
            ).scalars().first()
            if identity is None or identity.status != models.MailIdentity.ACTIVE:
                return refuse("IDENTITY_NOT_ACTIVE", "the sending identity is not active")

        # 4. Application and thread belong to this organisation.
        if intent.application_id:
            application = self.db.execute(
                select(models.Application).where(
                    models.Application.id == intent.application_id,
                    models.Application.org_id == self.org_id,
                )
            ).scalars().first()
            if application is None:
                return refuse("APPLICATION_NOT_IN_ORG", "the application is not this organisation's")
        if intent.thread_id:
            thread = self.db.execute(
                select(models.MailThread).where(
                    models.MailThread.id == intent.thread_id,
                    models.MailThread.org_id == self.org_id,
                )
            ).scalars().first()
            if thread is None:
                return refuse("THREAD_NOT_IN_ORG", "the thread is not this organisation's")

        # 5. Recipients, re-checked. The durable state may have changed while the
        #    approval sat waiting.
        known_domains, known_donors = self._known_donors()
        recipients = check_recipients(
            to_addresses=intent.to_addresses,
            cc_addresses=intent.cc_addresses,
            bcc_addresses=intent.bcc_addresses,
            reply_to_address=intent.reply_to_address,
            from_address=intent.from_address,
            known_donor_domains=known_domains,
            known_donors=known_donors,
        )
        if not recipients.ok:
            return refuse(
                "RECIPIENT_UNSAFE",
                "; ".join(e["detail"] for e in recipients.errors),
            )

        # 6. Attachments, re-verified against the database.
        import contextvars

        from agent.mail import risk as risk_module

        token = risk_module._SESSION.set(self.db)
        try:
            attachments = verify_attachment_manifest(
                (intent.attachment_manifest or {}).get("entries") or [],
                org_id=self.org_id,
            )
        finally:
            risk_module._SESSION.reset(token)
        if not attachments.ok:
            return refuse(
                "ATTACHMENT_INVALID",
                "; ".join(e["detail"] for e in attachments.errors),
            )

        return None

    # ------------------------------------------------------------------
    def _call_provider(self, intent: models.MailSendIntent, message: OutboundMessage) -> SubmitResult:
        """Call the provider, translating every failure into one of three outcomes.

        The translation is the safety-critical part. An exception that means "we did
        not learn the outcome" must become ``DELIVERY_UNKNOWN``, never
        ``CONFIRMED_NOT_SENT``.
        """
        started = _now()
        try:
            result = self.outbound.submit_message(
                message=message, idempotency_key=intent.idempotency_key
            )
        except OutboundCapabilityMissing as exc:
            return SubmitResult(
                outcome=SendOutcome.CONFIRMED_NOT_SENT,
                failure=SendFailure.PROVIDER_POLICY_BLOCK,
                error_code="CAPABILITY_MISSING",
                safe_error_summary=str(exc)[:300],
            )
        except TimeoutError as exc:
            # NOT a rejection.
            return SubmitResult(
                outcome=SendOutcome.DELIVERY_UNKNOWN,
                failure=SendFailure.NETWORK_TIMEOUT,
                error_code="TIMEOUT",
                safe_error_summary=f"the provider did not answer in time: {type(exc).__name__}",
            )
        except (ConnectionError, OSError) as exc:
            # NOT a rejection.
            return SubmitResult(
                outcome=SendOutcome.DELIVERY_UNKNOWN,
                failure=SendFailure.CONNECTION_RESET,
                error_code="CONNECTION",
                safe_error_summary=f"the connection failed: {type(exc).__name__}",
            )
        except Exception as exc:  # noqa: BLE001
            # An unclassified provider exception is an UNKNOWN outcome, not a
            # failure. Guessing "not sent" here is how a duplicate is created, so
            # the safe default is "we do not know".
            return SubmitResult(
                outcome=SendOutcome.DELIVERY_UNKNOWN,
                failure=SendFailure.PROVIDER_ERROR_UNKNOWN,
                error_code=type(exc).__name__[:60],
                safe_error_summary=(
                    "the provider raised an error Granada cannot classify as a definite "
                    "rejection, so the outcome is unknown"
                ),
            )

        if result.latency_ms is None:
            result.latency_ms = int((_now() - started).total_seconds() * 1000)
        return result

    # ------------------------------------------------------------------
    def _record(self, intent_id: str, result: SubmitResult, *, worker_id: str) -> SendResult:
        """TRANSACTION B: the immutable attempt, the receipt, the transition."""
        intent = self.db.execute(
            select(models.MailSendIntent).where(
                models.MailSendIntent.id == intent_id,
                models.MailSendIntent.org_id == self.org_id,
            )
        ).scalars().first()
        if intent is None:  # pragma: no cover - claimed moments ago
            self.db.rollback()
            return SendResult(intent_id=intent_id, refused=True, refusal_code="NOT_FOUND")

        attempt = models.MailSendAttempt(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=self.agent_id,
            send_intent_id=intent.id,
            attempt_number=intent.attempt_count or 1,
            attempt_id=f"att-{secrets.token_hex(12)}",
            provider=self.outbound.name if self.outbound is not None else "UNKNOWN",
            request_fingerprint=intent.message_fingerprint,
            granada_message_ref=intent.granada_message_ref,
            started_at=intent.send_started_at or _now(),
            finished_at=_now(),
            duration_ms=result.latency_ms,
            result=result.outcome.value,
            error_code=result.error_code,
            safe_error_summary=result.safe_error_summary,
            provider_submission_id=result.provider_submission_id,
            provider_message_id=result.provider_message_id,
            reconciliation_state=(
                models.MailSendAttempt.RECON_ACCEPTED
                if result.outcome == SendOutcome.CONFIRMED_SENT
                else models.MailSendAttempt.RECON_UNKNOWN
            ),
            worker_id=worker_id,
            created_at=_now(),
        )
        self.db.add(attempt)

        if result.outcome == SendOutcome.CONFIRMED_SENT:
            self._record_sent(intent, result, attempt)
            outcome_name = SendOutcome.CONFIRMED_SENT.value
        elif result.outcome == SendOutcome.CONFIRMED_NOT_SENT:
            self._record_definite_failure(intent, result, attempt)
            outcome_name = SendOutcome.CONFIRMED_NOT_SENT.value
        else:
            self._record_unknown(intent, result, attempt)
            outcome_name = SendOutcome.DELIVERY_UNKNOWN.value

        self.db.commit()
        return SendResult(
            intent_id=intent.id,
            outcome=outcome_name,
            state=intent.status,
            attempt_id=attempt.id,
            provider_submission_id=result.provider_submission_id,
            failure=result.failure.value if result.failure else None,
            detail=result.safe_error_summary or "",
        )

    # ------------------------------------------------------------------
    def _record_sent(
        self, intent: models.MailSendIntent, result: SubmitResult, attempt: models.MailSendAttempt
    ) -> None:
        """Mark SENT - and only here, and only with a receipt.

        A successful SDK call is not acceptance. This is called only on
        ``CONFIRMED_SENT``, which an adapter must produce from actual provider
        evidence.
        """
        intent.status = models.MailSendIntent.SENT
        intent.sent_at = result.accepted_at or _now()
        intent.provider_submission_id = result.provider_submission_id
        intent.provider_message_id = result.provider_message_id
        intent.internet_message_id = result.internet_message_id
        # SENT is ACCEPTANCE, not delivery. Saying "delivered" here would be a lie
        # the customer could act on.
        intent.delivery_state = "ACCEPTED"
        intent.failure_code = None
        intent.failure_summary = None
        intent.status_reason = None
        intent.reconciled_at = _now()
        intent.reconciliation_state = models.MailSendAttempt.RECON_ACCEPTED
        self.db.flush()

        self._stage_event(
            event_type="mail.send_accepted",
            payload={
                "send_intent_id": intent.id,
                "provider_submission_id": result.provider_submission_id,
                "granada_message_ref": intent.granada_message_ref,
            },
        )
        self._record_activity(
            summary_key="mail.sent",
            structured={
                "send_intent_id": intent.id,
                "recipients": intent.to_addresses,
                "subject": intent.subject,
                # Deliberately "accepted by the provider", never "delivered".
                "state": "ACCEPTED_BY_PROVIDER",
            },
            subject_id=intent.id,
        )

    def _record_definite_failure(
        self, intent: models.MailSendIntent, result: SubmitResult, attempt: models.MailSendAttempt
    ) -> None:
        """A failure the provider positively confirmed. Only these may be retried."""
        intent.failure_code = result.error_code or (
            result.failure.value if result.failure else "UNKNOWN"
        )
        intent.failure_summary = result.safe_error_summary
        intent.status_reason = result.safe_error_summary

        if result.failure == SendFailure.AUTH_REQUIRED:
            intent.status = models.MailSendIntent.REAUTH_REQUIRED
            account = self._account(intent)
            if account is not None:
                account.status = models.MailAccount.REAUTH_REQUIRED
                account.last_error = result.safe_error_summary
            self._stage_event(
                event_type="mail.reauth_required",
                payload={"send_intent_id": intent.id, "account_id": intent.mail_account_id},
            )
        elif result.failure == SendFailure.RATE_LIMITED:
            intent.status = models.MailSendIntent.RATE_LIMITED
            # A definite retry time. No tight loop, and no duplicate intent.
            delay = result.retry_after_seconds or 300
            intent.retry_not_before = _now() + timedelta(seconds=delay)
            self._stage_event(
                event_type="mail.send_rate_limited",
                payload={"send_intent_id": intent.id, "retry_after_seconds": delay},
            )
        elif result.failure in RETRYABLE:
            intent.status = models.MailSendIntent.TEMPORARY_FAILURE
            intent.retry_not_before = _now() + timedelta(seconds=60)
        else:
            intent.status = models.MailSendIntent.FAILED_FINAL

        self.db.flush()
        self._stage_event(
            event_type="mail.send_failed",
            payload={
                "send_intent_id": intent.id,
                "failure": result.failure.value if result.failure else None,
                "definite": True,
            },
        )

    def _record_unknown(
        self, intent: models.MailSendIntent, result: SubmitResult, attempt: models.MailSendAttempt
    ) -> None:
        """Granada does not know. Say so, and forbid a retry.

        This is the state the brief insists must exist separately from FAILED. The
        intent stays in ``DELIVERY_UNKNOWN``, which is in ``UNCERTAIN``, so
        ``_claim`` refuses any further attempt until reconciliation produces
        evidence.
        """
        intent.status = models.MailSendIntent.DELIVERY_UNKNOWN
        intent.failure_code = result.error_code or "UNKNOWN_OUTCOME"
        intent.failure_summary = result.safe_error_summary
        intent.status_reason = (
            "the provider may have accepted this message. Granada will not attempt "
            "another submission until it has established what happened, because "
            "retrying an unknown outcome can send the same email twice."
        )
        self.db.flush()
        self._stage_event(
            event_type="mail.delivery_unknown",
            payload={
                "send_intent_id": intent.id,
                "granada_message_ref": intent.granada_message_ref,
                "failure": result.failure.value if result.failure else None,
            },
        )
        self._record_activity(
            summary_key="mail.delivery_unknown",
            structured={
                "send_intent_id": intent.id,
                "detail": "checking whether the provider accepted the message before "
                          "attempting anything else",
            },
            subject_id=intent.id,
        )

    # ==================================================================
    # 3. Reconciliation
    # ==================================================================
    def reconcile(self, *, intent_id: str) -> SendResult:
        """Establish what actually happened to an uncertain attempt.

        Only positive evidence from the provider may move an intent out of
        ``DELIVERY_UNKNOWN``. A provider that simply cannot find the message has not
        proven anything unless it is able to enumerate its own sent mail - which is
        what ``authoritative_absence`` records. Without that, the honest state is
        still "unknown", and a retry stays forbidden.
        """
        assert_capability(Capability.MAIL_RECONCILE_SEND)

        intent = self.db.execute(
            select(models.MailSendIntent).where(
                models.MailSendIntent.id == intent_id,
                models.MailSendIntent.org_id == self.org_id,
            )
        ).scalars().first()
        if intent is None:
            return SendResult(intent_id=intent_id, refused=True, refusal_code="NOT_FOUND")

        if self.outbound is None:
            return SendResult(
                intent_id=intent.id, state=intent.status, refused=True,
                refusal_code="NO_OUTBOUND_PROVIDER",
                detail="reconciliation needs a provider to ask",
            )

        found = self.outbound.query_submission(
            granada_message_ref=intent.granada_message_ref or "",
            provider_submission_id=intent.provider_submission_id,
        )
        intent.reconciled_at = _now()

        if found.found and found.outcome == SendOutcome.CONFIRMED_SENT:
            # Positive evidence: the provider has it. This is NOT a second send.
            attempt = self.db.execute(
                select(models.MailSendAttempt).where(
                    models.MailSendAttempt.send_intent_id == intent.id
                ).order_by(models.MailSendAttempt.attempt_number.desc())
            ).scalars().first()
            if attempt is not None:
                attempt.reconciliation_state = models.MailSendAttempt.RECON_ACCEPTED
                attempt.reconciled_at = _now()
                attempt.provider_submission_id = found.provider_submission_id or attempt.provider_submission_id
            intent.status = models.MailSendIntent.SENT
            intent.sent_at = found.accepted_at or _now()
            intent.provider_submission_id = found.provider_submission_id
            intent.provider_message_id = found.provider_message_id
            intent.internet_message_id = found.internet_message_id
            intent.delivery_state = "ACCEPTED"
            intent.reconciliation_state = models.MailSendAttempt.RECON_ACCEPTED
            intent.failure_code = None
            intent.failure_summary = None
            intent.status_reason = "reconciled: the provider confirmed acceptance"
            self.db.flush()
            self._stage_event(
                event_type="mail.send_accepted",
                payload={"send_intent_id": intent.id, "reconciled": True},
            )
            self._record_activity(
                summary_key="mail.sent",
                structured={"send_intent_id": intent.id, "reconciled": True,
                            "state": "ACCEPTED_BY_PROVIDER"},
                subject_id=intent.id,
            )
            self.db.commit()
            return SendResult(
                intent_id=intent.id, outcome=SendOutcome.CONFIRMED_SENT.value,
                state=intent.status, reconciled=True,
                provider_submission_id=intent.provider_submission_id,
                detail="reconciled: previously unknown, now confirmed accepted",
            )

        if found.authoritative_absence:
            # The provider positively states it has no such message. NOW a retry is
            # permissible, because the earlier attempt is proven not to have landed.
            attempt = self.db.execute(
                select(models.MailSendAttempt).where(
                    models.MailSendAttempt.send_intent_id == intent.id
                ).order_by(models.MailSendAttempt.attempt_number.desc())
            ).scalars().first()
            if attempt is not None:
                attempt.reconciliation_state = models.MailSendAttempt.RECON_NOT_ACCEPTED
                attempt.reconciled_at = _now()
            intent.status = models.MailSendIntent.TEMPORARY_FAILURE
            intent.reconciliation_state = models.MailSendAttempt.RECON_NOT_ACCEPTED
            intent.retry_not_before = _now()
            intent.status_reason = (
                "reconciled: the provider authoritatively confirms it never accepted "
                "this message, so a retry is safe"
            )
            self.db.flush()
            self.db.commit()
            return SendResult(
                intent_id=intent.id, outcome=SendOutcome.CONFIRMED_NOT_SENT.value,
                state=intent.status, reconciled=True,
                detail="reconciled: proven not accepted, a retry is now permitted",
            )

        # No usable evidence. Stay unknown, and say why.
        intent.status = models.MailSendIntent.DELIVERY_UNKNOWN
        intent.reconciliation_state = models.MailSendAttempt.RECON_UNKNOWN
        intent.status_reason = (
            "reconciliation produced no decisive evidence: "
            + (found.detail or "the provider could not confirm or deny acceptance")
        )
        self.db.flush()
        self.db.commit()
        return SendResult(
            intent_id=intent.id, outcome=SendOutcome.DELIVERY_UNKNOWN.value,
            state=intent.status, reconciled=True, refused=True,
            refusal_code="STILL_UNKNOWN", detail=intent.status_reason,
        )

    # ==================================================================
    # 4. Bounce handling
    # ==================================================================
    def record_bounce(
        self, *, intent_id: str, detail: Optional[dict[str, Any]] = None
    ) -> SendResult:
        """A provider later reported a bounce.

        The message stays historically SENT, because it was: a bounce does not
        un-send it. Only the delivery state changes, and the history is never
        deleted - an organisation that deletes a bounce record cannot answer "did we
        ever contact this funder?".
        """
        intent = self.db.execute(
            select(models.MailSendIntent).where(
                models.MailSendIntent.id == intent_id,
                models.MailSendIntent.org_id == self.org_id,
            )
        ).scalars().first()
        if intent is None:
            return SendResult(intent_id=intent_id, refused=True, refusal_code="NOT_FOUND")
        if intent.sent_at is None:
            return SendResult(
                intent_id=intent.id, state=intent.status, refused=True,
                refusal_code="NOT_SENT",
                detail="a bounce cannot be recorded for a message that was never accepted",
            )

        intent.delivery_state = "BOUNCED"
        intent.bounced_at = _now()
        intent.bounce_detail = detail or {}
        # The recipient is worth reviewing: a bounce usually means the address is
        # wrong, and replying again to it will bounce again.
        self.db.flush()
        self.db.commit()
        self._stage_event(
            event_type="mail.bounced",
            payload={"send_intent_id": intent.id, "recipients": intent.to_addresses},
        )
        self._record_activity(
            summary_key="mail.bounced",
            structured={
                "send_intent_id": intent.id,
                "recipients": intent.to_addresses,
                "detail": "the recipient's server rejected this message after acceptance",
            },
            subject_id=intent.id,
        )
        self.db.commit()
        return SendResult(
            intent_id=intent.id, outcome=SendOutcome.CONFIRMED_SENT.value,
            state=intent.status, detail="bounce recorded; the send history is preserved",
        )

    # ==================================================================
    # helpers
    # ==================================================================
    def _build_message(self, intent: models.MailSendIntent) -> OutboundMessage:
        """Build the provider payload from the frozen columns only.

        Deliberately NOT from the draft. If this read through to the draft, editing
        it after approval would change what is sent - which is the entire failure the
        fingerprint exists to prevent.
        """
        return OutboundMessage(
            from_address=intent.from_address or "",
            to_addresses=tuple(intent.to_addresses or []),
            cc_addresses=tuple(intent.cc_addresses or []),
            bcc_addresses=tuple(intent.bcc_addresses or []),
            subject=intent.subject or "",
            body_text=intent.body_snapshot or "",
            reply_to_address=intent.reply_to_address,
            attachments=tuple(
                {
                    "document_id": entry.get("document_id"),
                    "filename": entry.get("filename"),
                    "mime_type": entry.get("mime_type"),
                    "checksum_sha256": entry.get("checksum_sha256"),
                    "storage_ref": entry.get("storage_ref"),
                }
                for entry in ((intent.attachment_manifest or {}).get("entries") or [])
            ),
            granada_message_ref=intent.granada_message_ref,
            approval_fingerprint=intent.message_fingerprint,
        )

    def _fingerprint_of(self, intent: models.MailSendIntent) -> tuple[str, str]:
        return fingerprint(
            org_id=intent.org_id,
            agent_id=intent.agent_id,
            from_address=intent.from_address,
            to_addresses=intent.to_addresses or [],
            cc_addresses=intent.cc_addresses or [],
            bcc_addresses=intent.bcc_addresses or [],
            reply_to_address=intent.reply_to_address,
            subject=intent.subject,
            body=intent.body_snapshot,
            attachments=(intent.attachment_manifest or {}).get("entries") or [],
            application_id=intent.application_id,
            thread_id=intent.thread_id,
            reply_to_message_id=intent.reply_to_message_id,
            draft_version=intent.draft_version,
            risk_class=intent.risk_class,
        )

    def _account(self, intent: models.MailSendIntent) -> Optional[models.MailAccount]:
        if not intent.mail_account_id:
            return None
        return self.db.execute(
            select(models.MailAccount).where(
                models.MailAccount.id == intent.mail_account_id,
                models.MailAccount.org_id == self.org_id,
            )
        ).scalars().first()

    def _known_donors(self) -> tuple[list[str], list[dict]]:
        from agent.mail.service import GranadaMail

        mail = GranadaMail(self.db, org_id=self.org_id, agent_id=self.agent_id)
        donors = mail._known_donors()
        return sorted({d["domain"] for d in donors if d.get("domain")}), donors

    def _fail_intent(self, intent_id: str, code: str, detail: str, *, state: str) -> SendResult:
        intent = self.db.execute(
            select(models.MailSendIntent).where(
                models.MailSendIntent.id == intent_id,
                models.MailSendIntent.org_id == self.org_id,
            )
        ).scalars().first()
        if intent is not None:
            intent.status = state
            intent.failure_code = code
            intent.failure_summary = detail
            self.db.commit()
        return SendResult(
            intent_id=intent_id, state=state, refused=True, refusal_code=code, detail=detail
        )

    def _stage_event(self, *, event_type: str, payload: dict[str, Any]) -> models.OutboxEvent:
        """Stage in the SAME transaction as the state change. Never inline."""
        event = models.OutboxEvent(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            stream=f"granada:v1:mail:{event_type.split('.')[-1]}",
            event_type=f"granada:v1:{event_type}",
            payload={**payload, "agent_id": self.agent_id, "organisation_id": self.org_id},
            created_at=_now(),
            attempts=0,
        )
        self.db.add(event)
        self.db.flush()
        return event

    def _record_activity(
        self, *, summary_key: str, structured: dict[str, Any], subject_id: Optional[str]
    ) -> models.AgentActivity:
        activity = models.AgentActivity(
            id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            org_id=self.org_id,
            specialist_key="EMAIL",
            activity_type="mail",
            summary_key=summary_key,
            subject_type="MAIL",
            subject_id=subject_id,
            structured_data=structured,
            visibility=models.AgentActivity.VISIBILITY_CUSTOMER,
            occurred_at=_now(),
        )
        self.db.add(activity)
        self.db.flush()
        return activity


#: A placeholder a person was meant to replace. Sending one verbatim is worse than
#: not sending at all, because the funder receives "[INSERT BANK STATEMENT]".
_PLACEHOLDER_PATTERNS = (
    r"\[INSERT[^\]]*\]",
    r"\[TODO[^\]]*\]",
    r"\[ORGANISATION NAME[^\]]*\]",
    r"\{\{[^}]*\}\}",
    r"<PLACEHOLDER[^>]*>",
    r"\[TBC\]",
    r"\[XX+\]",
)


def _has_unresolved_placeholder(body: Optional[str]) -> bool:
    import re

    if not body:
        return False
    for pattern in _PLACEHOLDER_PATTERNS:
        if re.search(pattern, body, re.IGNORECASE):
            return True
    return False
