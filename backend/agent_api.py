"""The agent and mail HTTP surface, including the approval-review contract.

Until now the agent and mail capabilities existed only as services and CLI commands.
That is fine for a fleet and useless for a product: the brief's §6 requires that "the
person must know exactly what approval authorises", and there was no contract that
told them.

The review endpoint is the important one
----------------------------------------
``GET /agent/mail/send-intents/{id}/review`` returns **everything** a person needs to
decide, and it is deliberately verbose:

From, To, CC, BCC, Subject, the exact body that will be sent, the attachment manifest,
the donor, the application, the thread, the risk class and its reasons, the facts used,
the documents used, the draft version, the agent, any deadline, the warnings, and the
suspicious recipient or domain indicators.

The brief is explicit about why: never present "Approve this conversation" when the
approval actually authorises sending a message the person has not seen. So this
endpoint returns the message itself - the snapshot, not the draft - and the client is
expected to render it. The fingerprint is included so a client can display what is
being authorised rather than paraphrase it.

Nothing here can send
---------------------
The approve endpoint records a decision. Whether a message then goes out is decided by
the send pipeline's final authority check, which reloads everything. An approval is a
permission, not an execution.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

import models
from agent.granada_agent import GranadaAgentService
from agent.mail.approval import (
    APPROVE_SEND_PERMISSION,
    ApprovalError,
    ApprovalService,
    NotPermitted,
    has_permission,
)
from agent.mail.autonomy import (
    AUTONOMOUS_CLASSIFICATION_ALLOWLIST,
    AUTONOMOUS_RISK_ALLOWLIST,
    platform_autonomy_enabled,
)
from agent.mail.ceiling import HIGH_RISK_CLASSES, OutboundRisk
from agent.mail.send_service import SendService
from agent.mail.service import GranadaMail
from database import get_db
from router import get_current_user, get_tenant_context, require_org_access
from tenant_context import TenantContext
from events.ledger import JobLedger

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["Granada Agent"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _agent_for(db: Session, org_id: str):
    """The organisation's Granada agent, or None if it has not been provisioned.

    Returns None rather than raising: an organisation that has never used the platform
    has no agent, and a dashboard asking about its grants should render an empty state
    rather than a 500.
    """
    try:
        return GranadaAgentService(db, org_id).get()
    except Exception:  # noqa: BLE001 - not provisioned yet is a normal state
        return None


def _organisation(tenant: TenantContext) -> str:
    if not tenant.primary_org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "this account belongs to several organisations, so no tenant is bound. "
                "Select an organisation context before using the agent API."
            ),
        )
    return tenant.primary_org_id


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ApprovalDecisionRequest(BaseModel):
    note: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="An optional note, recorded with the decision and shown to colleagues.",
    )


class SendIntentSummary(BaseModel):
    id: str
    status: str
    status_reason: Optional[str]
    risk_class: str
    subject: Optional[str]
    from_address: Optional[str]
    to_addresses: list[str]
    application_id: Optional[str]
    thread_id: Optional[str]
    draft_version: Optional[int]
    attempt_count: int
    created_at: Optional[str]
    approved_at: Optional[str]
    sent_at: Optional[str]
    delivery_state: Optional[str]
    approval_currently_authorises_this_message: bool


class SendIntentReview(SendIntentSummary):
    """The full contract. Every field the brief requires, and nothing hidden."""

    cc_addresses: list[str]
    bcc_addresses: list[str]
    reply_to_address: Optional[str]
    #: THE EXACT BYTES. The snapshot, not the draft - if this read through to the
    #: draft, editing it after approval would change what gets sent.
    body: Optional[str]
    attachment_manifest: list[dict[str, Any]]
    message_fingerprint: str
    #: Everything a person needs to judge the risk, in the words the classifier used.
    risk: dict[str, Any]
    #: What the draft asserts and where each assertion came from.
    facts_used: list[dict[str, Any]]
    documents_used: list[dict[str, Any]]
    donor: Optional[str]
    application: Optional[dict[str, Any]]
    thread: Optional[dict[str, Any]]
    deadline: Optional[dict[str, Any]]
    agent: dict[str, Any]
    warnings: list[dict[str, str]]
    risky_recipients: list[dict[str, str]]
    approvals: list[dict[str, Any]]
    #: Whether the CALLER may approve this. Sent so a client can grey the button
    #: rather than let somebody click and be refused.
    caller_may_approve: bool
    caller_may_approve_reason: Optional[str]


# ---------------------------------------------------------------------------
# Status and health
# ---------------------------------------------------------------------------
@router.get("", summary="Your Granada Agent")
def agent_status(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict[str, Any]:
    """The customer-facing panel: what the agent has done, and what needs a person."""
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    service = GranadaAgentService(db, org_id)
    agent = service.get()
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no Granada Agent has been provisioned for this organisation",
        )

    agent_status_panel = service.status()
    mail_status = GranadaMail(db, org_id=org_id, agent_id=agent.id).status()
    allowed, reason = has_permission(
        db, org_id=org_id, user_id=user.id, permission=APPROVE_SEND_PERMISSION
    )

    return {
        **agent_status_panel.as_dict(),
        "mail": mail_status,
        "permissions": {
            "may_approve_outbound_mail": allowed,
            "reason": reason,
        },
        "autonomy": {
            # Both switches, so "why is my agent not sending by itself?" has both
            # possible answers rather than one number that sends an operator looking
            # in the wrong place.
            "platform_enabled": platform_autonomy_enabled(),
            "organisation_enabled": bool((agent.settings or {}).get("autonomous_mail_enabled")),
            "risk_allowlist": sorted(AUTONOMOUS_RISK_ALLOWLIST),
            "classification_allowlist": sorted(AUTONOMOUS_CLASSIFICATION_ALLOWLIST),
            "high_risk_always_refused": True,
        },
    }



# ===========================================================================
# DELIVERY — what the organisation owes, and what it is owed
# ===========================================================================
class GrantSummary(BaseModel):
    id: str
    reference: str
    title: str
    donor_name: Optional[str] = None
    currency: str
    awarded_amount: Optional[float] = None
    requested_amount: Optional[float] = None
    size_relative_to_request: Optional[str] = None
    status: str
    awarded_at: Optional[str] = None
    starts_on: Optional[str] = None
    ends_on: Optional[str] = None
    # Provenance: which authorised package every figure came from.
    source_package_id: Optional[str] = None
    application_id: Optional[str] = None


class DeadlineSummary(BaseModel):
    kind: str
    id: str
    title: str
    due_on: str
    days_remaining: int
    overdue: bool
    blocks_payment: bool = False
    grant_id: Optional[str] = None


@router.get("/grants", summary="Grants this organisation holds")
def list_grants(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
    status_filter: Optional[str] = None,
    limit: int = 100,
) -> dict[str, Any]:
    """The portfolio.

    Amounts are returned as numbers rather than strings so a client does not have to
    parse money, and ``size_relative_to_request`` is included because an award smaller
    than the request changes the whole workplan - and it is invisible from the amount
    alone.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    statement = select(models.Grant).where(models.Grant.org_id == org_id)
    if status_filter:
        statement = statement.where(models.Grant.status == status_filter)
    statement = statement.order_by(models.Grant.created_at.desc()).limit(max(1, min(limit, 500)))

    rows = db.execute(statement).scalars().all()
    return {
        "organisation_id": org_id,
        "count": len(rows),
        "grants": [
            GrantSummary(
                id=g.id,
                reference=g.reference,
                title=g.title,
                donor_name=g.donor_name,
                currency=g.currency,
                awarded_amount=float(g.awarded_amount) if g.awarded_amount is not None else None,
                requested_amount=float(g.requested_amount) if g.requested_amount is not None else None,
                size_relative_to_request=g.size_relative_to_request,
                status=g.status,
                awarded_at=_iso(g.awarded_at),
                starts_on=_iso(g.starts_on),
                ends_on=_iso(g.ends_on),
                source_package_id=g.source_package_id,
                application_id=g.application_id,
            ).model_dump()
            for g in rows
        ],
    }


@router.get("/grants/{grant_id}", summary="One grant, with its obligations")
def get_grant(
    grant_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict[str, Any]:
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    grant = db.execute(
        select(models.Grant).where(
            models.Grant.id == grant_id, models.Grant.org_id == org_id
        )
    ).scalars().first()
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such grant")

    conditions = db.execute(
        select(models.GrantCondition).where(models.GrantCondition.grant_id == grant.id)
    ).scalars().all()
    obligations = db.execute(
        select(models.ReportingObligation).where(
            models.ReportingObligation.grant_id == grant.id
        )
    ).scalars().all()
    tranches = db.execute(
        select(models.Disbursement).where(models.Disbursement.grant_id == grant.id)
    ).scalars().all()
    project = db.execute(
        select(models.Project).where(models.Project.grant_id == grant.id)
    ).scalars().first()

    return {
        "grant": GrantSummary(
            id=grant.id, reference=grant.reference, title=grant.title,
            donor_name=grant.donor_name, currency=grant.currency,
            awarded_amount=float(grant.awarded_amount) if grant.awarded_amount is not None else None,
            requested_amount=float(grant.requested_amount) if grant.requested_amount is not None else None,
            size_relative_to_request=grant.size_relative_to_request,
            status=grant.status, awarded_at=_iso(grant.awarded_at),
            starts_on=_iso(grant.starts_on), ends_on=_iso(grant.ends_on),
            source_package_id=grant.source_package_id, application_id=grant.application_id,
        ).model_dump(),
        "project": None if project is None else {
            "id": project.id, "name": project.name, "status": project.status,
            "budget_total": float(project.budget_total) if project.budget_total is not None else None,
            "baseline_workplan": project.baseline_workplan,
        },
        "conditions": [
            {
                "id": c.id, "title": c.title, "kind": c.kind, "status": c.status,
                "blocks_payment": c.blocks_payment, "due_on": _iso(c.due_on),
                # Present only when it truly was satisfied - the service refuses without
                # it, so a client can treat a null here as "not actually evidenced".
                "evidence_ref": c.evidence_ref, "satisfied_at": _iso(c.satisfied_at),
            }
            for c in conditions
        ],
        "reporting_obligations": [
            {
                "id": o.id, "title": o.title, "kind": o.kind, "period": o.period,
                "status": o.status, "due_on": _iso(o.due_on),
                "submitted_at": _iso(o.submitted_at), "reference": o.reference,
                "remind_days_before": o.remind_days_before,
            }
            for o in obligations
        ],
        "disbursements": [
            {
                "id": d.id, "label": d.label, "tranche_number": d.tranche_number,
                "status": d.status, "amount": float(d.amount) if d.amount is not None else None,
                "amount_received": (
                    float(d.amount_received) if d.amount_received is not None else None
                ),
                "currency": d.currency, "expected_on": _iso(d.expected_on),
                "received_on": _iso(d.received_on), "reference": d.reference,
                "variance_note": d.variance_note,
            }
            for d in tranches
        ],
    }


@router.get("/deadlines", summary="Everything with a date, soonest first")
def list_deadlines(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
    within_days: int = 30,
) -> dict[str, Any]:
    """Conditions, reports and tranches together.

    Deliberately one list from all three sources: an organisation's obligations are not
    separated by which table they live in, and a view showing only reports would hide the
    precondition blocking the next payment.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    agent = _agent_for(db, org_id)
    if agent is None:
        return {"organisation_id": org_id, "count": 0, "deadlines": []}

    from agent.delivery.service import DeliveryService

    service = DeliveryService(db, org_id=org_id, agent_id=agent.id)
    found = service.deadlines(within_days=max(1, min(within_days, 365)))
    return {
        "organisation_id": org_id,
        "within_days": within_days,
        "count": len(found),
        "deadlines": [d.as_dict() for d in found],
    }


@router.get("/compliance", summary="What is at risk right now")
def compliance(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict[str, Any]:
    """Blocked payments, overdue reports and late money, as three separate lists.

    Not a score. A score would require weighting a blocked payment against a late report,
    and the right response to each is different: one is a phone call, the other is
    writing.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    agent = _agent_for(db, org_id)
    if agent is None:
        return {
            "organisation_id": org_id, "grants_active": 0,
            "blocking_conditions": [], "overdue_reports": [], "late_disbursements": [],
            "portfolio": {"scheduled_total": "0", "received_total": "0", "outstanding_total": "0"},
            "counts": {"blocking_conditions": 0, "overdue_reports": 0, "late_disbursements": 0},
        }

    from agent.delivery.service import DeliveryService

    service = DeliveryService(db, org_id=org_id, agent_id=agent.id)
    summary = service.compliance_summary()
    # Persist the status transitions the scan just derived, so a later read is
    # consistent rather than re-deriving from a different clock.
    db.commit()
    return {"organisation_id": org_id, **summary}


@router.get("/health", summary="Fleet, relay and autonomy health")
def agent_health(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict[str, Any]:
    """What an operator or a probe needs, without reading logs.

    Reports the outbox backlog, because a relay that is not running is invisible from
    the outside: PostgreSQL holds the truth either way, so nothing looks broken while
    events pile up undelivered.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    backlog = db.execute(
        select(models.OutboxEvent).where(models.OutboxEvent.published_at.is_(None))
    ).scalars().all()
    queued_jobs = db.execute(
        select(models.Job).where(models.Job.org_id == org_id, models.Job.state == models.Job.QUEUED)
    ).scalars().all()
    running_jobs = db.execute(
        select(models.Job).where(models.Job.org_id == org_id, models.Job.state == models.Job.RUNNING)
    ).scalars().all()
    unknown = db.execute(
        select(models.MailSendIntent).where(
            models.MailSendIntent.org_id == org_id,
            models.MailSendIntent.status == models.MailSendIntent.DELIVERY_UNKNOWN,
        )
    ).scalars().all()

    return {
        "organisation_id": org_id,
        "outbox": {
            # A non-zero backlog with no relay running means events are staged and
            # never delivered. Reported rather than inferred.
            "unpublished": len(backlog),
            "oldest_unpublished_at": _iso(
                min((e.created_at for e in backlog), default=None)
            ),
        },
        "fleet": {
            "jobs_queued": len(queued_jobs),
            "jobs_running": len(running_jobs),
        },
        "outbound": {
            "delivery_unknown": len(unknown),
            "awaiting_reconciliation": [i.id for i in unknown],
        },
    }


# ---------------------------------------------------------------------------
# Approval queue
# ---------------------------------------------------------------------------
@router.get("/mail/send-intents", summary="Outbound messages awaiting a decision")
def list_send_intents(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
    status_filter: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    stmt = select(models.MailSendIntent).where(models.MailSendIntent.org_id == org_id)
    if status_filter:
        stmt = stmt.where(models.MailSendIntent.status == status_filter)
    else:
        # The default is the queue that needs a person, because that is what the
        # screen is for.
        stmt = stmt.where(
            models.MailSendIntent.status.in_(
                [
                    models.MailSendIntent.WAITING_FOR_APPROVAL,
                    models.MailSendIntent.CHANGES_REQUESTED,
                ]
            )
        )
    rows = db.execute(
        stmt.order_by(models.MailSendIntent.created_at.asc()).limit(min(200, max(1, limit)))
    ).scalars().all()

    service = ApprovalService(db, org_id=org_id)
    summaries = []
    for intent in rows:
        live = service.active_approval(intent)
        summaries.append(
            SendIntentSummary(
                id=intent.id,
                status=intent.status,
                status_reason=intent.status_reason,
                risk_class=intent.risk_class,
                subject=intent.subject,
                from_address=intent.from_address,
                to_addresses=list(intent.to_addresses or []),
                application_id=intent.application_id,
                thread_id=intent.thread_id,
                draft_version=intent.draft_version,
                attempt_count=intent.attempt_count or 0,
                created_at=_iso(intent.created_at),
                approved_at=_iso(intent.approved_at),
                sent_at=_iso(intent.sent_at),
                delivery_state=intent.delivery_state,
                approval_currently_authorises_this_message=live is not None,
            ).model_dump()
        )
    return {"count": len(summaries), "send_intents": summaries}


@router.get(
    "/mail/send-intents/{intent_id}/review",
    response_model=SendIntentReview,
    summary="Everything a person needs to decide",
)
def review_send_intent(
    intent_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> SendIntentReview:
    """THE approval contract.

    Returns the message itself, not a description of it. The brief's rule governs the
    shape: never offer "Approve this conversation" when the approval actually
    authorises sending a message the person has not seen.

    Everything here is read from the FROZEN intent rather than from the draft, so what
    a person reviews is what would be sent even if the draft has since changed.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    intent = db.execute(
        select(models.MailSendIntent).where(
            models.MailSendIntent.id == intent_id, models.MailSendIntent.org_id == org_id
        )
    ).scalars().first()
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such send intent")

    service = ApprovalService(db, org_id=org_id)
    live = service.active_approval(intent)
    approvals = db.execute(
        select(models.MailApproval)
        .where(models.MailApproval.send_intent_id == intent.id)
        .order_by(models.MailApproval.approved_at.asc())
    ).scalars().all()

    risk_detail = intent.risk_detail or {}
    recipient_report = risk_detail.get("recipient_report") or {}
    attachment_report = risk_detail.get("attachment_report") or {}

    # The draft's provenance: which facts and documents it was built from.
    draft = None
    if intent.draft_id:
        draft = db.execute(
            select(models.MailDraft).where(models.MailDraft.id == intent.draft_id)
        ).scalars().first()
    facts_used = list(((draft.facts_used or {}).get("facts") if draft else []) or [])
    documents_used = list(((draft.documents_used or {}).get("documents") if draft else []) or [])

    donor = None
    application_payload = None
    if intent.application_id:
        application = db.execute(
            select(models.Application).where(
                models.Application.id == intent.application_id,
                models.Application.org_id == org_id,
            )
        ).scalars().first()
        if application is not None:
            opportunity = db.execute(
                select(models.Opportunity).where(
                    models.Opportunity.id == application.opportunity_id
                )
            ).scalars().first()
            donor = getattr(opportunity, "source_name", None)
            application_payload = {
                "id": application.id,
                "state": application.state,
                "version": getattr(application, "version", None),
                "opportunity_title": getattr(opportunity, "title", None),
                "opportunity_deadline": _iso(getattr(opportunity, "deadline", None)),
            }

    thread_payload = None
    if intent.thread_id:
        thread = db.execute(
            select(models.MailThread).where(
                models.MailThread.id == intent.thread_id,
                models.MailThread.org_id == org_id,
            )
        ).scalars().first()
        if thread is not None:
            thread_payload = {
                "id": thread.id,
                "status": thread.status,
                "first_message_at": _iso(thread.first_message_at),
                "last_message_at": _iso(thread.last_message_at),
                "message_count": len(
                    db.execute(
                        select(models.MailMessage.id).where(
                            models.MailMessage.thread_id == thread.id
                        )
                    ).all()
                ),
            }

    deadline_payload = None
    if intent.reply_to_message_id:
        deadline = db.execute(
            select(models.MailDeadline).where(
                models.MailDeadline.message_id == intent.reply_to_message_id,
                models.MailDeadline.org_id == org_id,
            )
        ).scalars().first()
        if deadline is not None:
            deadline_payload = {
                "id": deadline.id,
                "raw_expression": deadline.raw_expression,
                "resolved_at": _iso(deadline.resolved_at),
                "timezone_assumption": deadline.timezone_assumption,
                "confidence": deadline.confidence,
                "status": deadline.status,
            }

    agent = db.execute(
        select(models.GranadaAgent).where(
            models.GranadaAgent.id == intent.agent_id,
            models.GranadaAgent.org_id == org_id,
        )
    ).scalars().first()

    allowed, reason = has_permission(
        db, org_id=org_id, user_id=user.id, permission=APPROVE_SEND_PERMISSION
    )

    return SendIntentReview(
        id=intent.id,
        status=intent.status,
        status_reason=intent.status_reason,
        risk_class=intent.risk_class,
        subject=intent.subject,
        from_address=intent.from_address,
        to_addresses=list(intent.to_addresses or []),
        cc_addresses=list(intent.cc_addresses or []),
        bcc_addresses=list(intent.bcc_addresses or []),
        reply_to_address=intent.reply_to_address,
        # The snapshot. What a person reads here is what would leave.
        body=intent.body_snapshot,
        attachment_manifest=list((intent.attachment_manifest or {}).get("entries") or []),
        message_fingerprint=intent.message_fingerprint,
        risk={
            "class": intent.risk_class,
            # Named explicitly so a client does not have to know the list.
            "high_risk": intent.risk_class in {r.value for r in HIGH_RISK_CLASSES},
            "reasons": risk_detail.get("reasons") or [],
            "matched": risk_detail.get("matched") or [],
            "classification": risk_detail.get("classification"),
            "security_flags": risk_detail.get("security_flags") or [],
            "sendable_in_this_phase": intent.risk_class not in {r.value for r in HIGH_RISK_CLASSES},
        },
        facts_used=facts_used,
        documents_used=documents_used or list(attachment_report.get("manifest") or []),
        donor=donor,
        application=application_payload,
        thread=thread_payload,
        deadline=deadline_payload,
        agent={
            "id": getattr(agent, "id", None),
            "display_name": getattr(agent, "display_name", None),
            "status": getattr(agent, "status", None),
            "autonomy": getattr(agent, "autonomy", None),
            "version": getattr(agent, "version", None),
            # Surfaced so a reviewer can see whether a pause or a downgrade would
            # block the send before they spend time reading the message.
            "authority_changed_since_creation": (
                agent is not None
                and intent.agent_version is not None
                and agent.version != intent.agent_version
            ),
        },
        warnings=list(recipient_report.get("warnings") or []),
        risky_recipients=list(recipient_report.get("errors") or []),
        approvals=[
            {
                "id": a.id,
                "decision": a.decision,
                "by": a.approved_by,
                "at": _iso(a.approved_at),
                "status": a.status,
                "fingerprint": a.fingerprint,
                "matches_current_message": a.fingerprint == intent.message_fingerprint,
                "policy_evidence": a.policy_evidence,
                "note": a.note,
            }
            for a in approvals
        ],
        caller_may_approve=allowed,
        caller_may_approve_reason=reason,
        application_id=intent.application_id,
        thread_id=intent.thread_id,
        draft_version=intent.draft_version,
        attempt_count=intent.attempt_count or 0,
        created_at=_iso(intent.created_at),
        approved_at=_iso(intent.approved_at),
        sent_at=_iso(intent.sent_at),
        delivery_state=intent.delivery_state,
        approval_currently_authorises_this_message=live is not None,
    )


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
def _decide(action: str, intent_id: str, db: Session, org_id: str, user_id: str, note: Optional[str]):
    """Run one decision, mapping the service's refusals onto HTTP statuses.

    A refusal is a 409 rather than a 403 when it is about the message's state: the
    caller had permission and the message is not in a decidable state, and telling
    them "forbidden" would send them looking for a permission they already hold.
    """
    service = ApprovalService(db, org_id=org_id)
    try:
        if action == "approve":
            outcome = service.approve(intent_id=intent_id, user_id=user_id, note=note)
        elif action == "reject":
            outcome = service.reject(intent_id=intent_id, user_id=user_id, note=note)
        elif action == "request-changes":
            outcome = service.request_changes(intent_id=intent_id, user_id=user_id, note=note)
        else:  # pragma: no cover - exhaustive by construction
            raise HTTPException(status_code=400, detail=f"unknown action {action}")
    except NotPermitted as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ApprovalError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a policy refusal carries its own code
        code = getattr(exc, "code", None) or type(exc).__name__
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"{code}: {exc}"
        ) from exc

    db.commit()
    return {"ok": True, **outcome.as_dict()}


@router.post("/mail/send-intents/{intent_id}/approve", summary="Approve THIS message")
def approve_send_intent(
    intent_id: str,
    payload: ApprovalDecisionRequest = ApprovalDecisionRequest(),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict[str, Any]:
    """Authorise the exact message.

    The fingerprint is recomputed server-side from the live row, so a client cannot
    approve one message while a different one is sent. If the intent changed while it
    was being reviewed, this returns 409 and the reviewer must re-read it.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)
    return _decide("approve", intent_id, db, org_id, user.id, payload.note)


@router.post("/mail/send-intents/{intent_id}/reject", summary="Reject the message")
def reject_send_intent(
    intent_id: str,
    payload: ApprovalDecisionRequest = ApprovalDecisionRequest(),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict[str, Any]:
    """Refuse it. Needs membership only: anyone who can see it can stop it."""
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)
    return _decide("reject", intent_id, db, org_id, user.id, payload.note)


@router.post("/mail/send-intents/{intent_id}/request-changes", summary="Ask for a different message")
def request_changes_send_intent(
    intent_id: str,
    payload: ApprovalDecisionRequest = ApprovalDecisionRequest(),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict[str, Any]:
    """The current intent becomes unsendable, so nobody can wave it through later."""
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)
    return _decide("request-changes", intent_id, db, org_id, user.id, payload.note)


@router.post("/mail/send-intents/{intent_id}/cancel", summary="Cancel before it leaves")
def cancel_send_intent(
    intent_id: str,
    payload: ApprovalDecisionRequest = ApprovalDecisionRequest(),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict[str, Any]:
    """Stop it before it is sent. Refuses once it is SENDING: a message already handed
    to a provider cannot be recalled, and pretending otherwise would leave a
    cancelled-looking record for something that reaches a funder."""
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    intent = db.execute(
        select(models.MailSendIntent).where(
            models.MailSendIntent.id == intent_id,
            models.MailSendIntent.org_id == org_id,
        )
    ).scalars().first()
    if intent is None:
        raise HTTPException(status_code=404, detail="no such send intent")
    if intent.status in models.MailSendIntent.TERMINAL:
        raise HTTPException(status_code=409, detail=f"the message is already {intent.status}")
    if intent.status == models.MailSendIntent.SENDING:
        raise HTTPException(
            status_code=409,
            detail=(
                "the message is already with the provider and cannot be recalled; "
                "reconcile it once the outcome is known"
            ),
        )

    previous = intent.status
    intent.status = models.MailSendIntent.CANCELLED
    intent.status_reason = payload.note or "cancelled by a person"
    for approval in db.execute(
        select(models.MailApproval).where(
            models.MailApproval.send_intent_id == intent.id,
            models.MailApproval.status == models.MailApproval.STATUS_ACTIVE,
        )
    ).scalars():
        approval.status = models.MailApproval.STATUS_REVOKED
        approval.revoked_at = _now()
        approval.revoked_by = user.id
    db.commit()
    return {"ok": True, "send_intent_id": intent.id, "previous_status": previous,
            "status": intent.status}


@router.post("/mail/send-intents/{intent_id}/reconcile", summary="Find out what happened")
def reconcile_send_intent(
    intent_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict[str, Any]:
    """Ask the provider what happened to an uncertain attempt.

    **This never sends.** Its whole purpose is to turn "we do not know" into evidence,
    so that any later decision to retry rests on a fact rather than on a guess.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    from agent.mail.gateway import get_outbound_transport

    intent = db.execute(
        select(models.MailSendIntent).where(
            models.MailSendIntent.id == intent_id,
            models.MailSendIntent.org_id == org_id,
        )
    ).scalars().first()
    if intent is None:
        raise HTTPException(status_code=404, detail="no such send intent")

    transport = get_outbound_transport(intent.provider or "")
    if transport is None:
        raise HTTPException(
            status_code=409,
            detail=f"no outbound provider is configured for {intent.provider!r}",
        )
    service = SendService(db, org_id=org_id, agent_id=intent.agent_id, outbound=transport)
    result = service.reconcile(intent_id=intent.id)
    return result.as_dict()


@router.get("/mail/drafts", summary="Reply drafts")
def list_drafts(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
    limit: int = 50,
) -> dict[str, Any]:
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)
    rows = db.execute(
        select(models.MailDraft)
        .where(models.MailDraft.org_id == org_id)
        .order_by(models.MailDraft.created_at.desc())
        .limit(min(200, max(1, limit)))
    ).scalars().all()
    return {
        "count": len(rows),
        "drafts": [
            {
                "id": d.id,
                "status": d.status,
                "status_reason": d.status_reason,
                "subject": d.subject,
                "version": d.version,
                "application_id": d.application_id,
                "thread_id": d.thread_id,
                "edit_source": d.edit_source,
                "supersedes_id": d.supersedes_id,
                "created_at": _iso(d.created_at),
                "edited_at": _iso(d.edited_at),
                # The body is NOT included in a list response: a list is for choosing,
                # and shipping every body to render a table is both slow and a
                # needless spread of correspondence.
                "body_preview": (d.body or "")[:300],
            }
            for d in rows
        ],
    }


@router.get("/mail/drafts/{draft_id}", summary="One draft, in full")
def get_draft(
    draft_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict[str, Any]:
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)
    draft = db.execute(
        select(models.MailDraft).where(
            models.MailDraft.id == draft_id, models.MailDraft.org_id == org_id
        )
    ).scalars().first()
    if draft is None:
        raise HTTPException(status_code=404, detail="no such draft")
    return {
        "id": draft.id,
        "status": draft.status,
        "status_reason": draft.status_reason,
        "subject": draft.subject,
        "body": draft.body,
        "version": draft.version,
        "application_id": draft.application_id,
        "thread_id": draft.thread_id,
        "reply_to_message_id": draft.reply_to_message_id,
        "facts_used": draft.facts_used,
        "documents_used": draft.documents_used,
        "edit_source": draft.edit_source,
        "supersedes_id": draft.supersedes_id,
        "created_at": _iso(draft.created_at),
        "edited_at": _iso(draft.edited_at),
    }


@router.get("/mail/accounts", summary="Connected and managed mailboxes")
def list_mail_accounts(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
) -> dict[str, Any]:
    """Mailboxes and their status.

    ``credentials_ref`` is a pointer into the secret store and is included because an
    operator needs to know WHICH secret is attached. **No secret value is ever
    returned**, and none is stored: there is no column that could hold a provider
    password.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    from agent.mail.gateway import get_outbound_transport, get_transport

    rows = db.execute(
        select(models.MailAccount).where(models.MailAccount.org_id == org_id)
    ).scalars().all()
    return {
        "count": len(rows),
        "accounts": [
            {
                "id": a.id,
                "provider": a.provider,
                "address": a.address,
                "connection_type": a.connection_type,
                "status": a.status,
                "scopes": a.scopes,
                "has_credentials_ref": bool(a.credentials_ref),
                "last_sync_at": _iso(a.last_sync_at),
                "sync_status": a.sync_status,
                "last_error": a.last_error,
                # Whether each capability is configured, resolved live rather than
                # stored, so a missing transport is visible before somebody wonders
                # why nothing is being sent.
                "inbound_configured": get_transport(a.provider) is not None,
                "outbound_configured": get_outbound_transport(a.provider) is not None,
            }
            for a in rows
        ],
    }
