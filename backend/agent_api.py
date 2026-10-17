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
from sqlalchemy import and_, case, func, select
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
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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


@router.get("/packages", summary="Application packages this organisation holds")
def list_packages(
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    limit: int = 50,
) -> dict[str, Any]:
    """The application packages, with readiness evaluated NOW.

    READINESS IS EVALUATED AT READ TIME, not read from a stored column. A package assembled while the
    opportunity was open can become blocked because the deadline passed, and a stored verdict would
    keep reporting the answer that was true when the job ran.

    The verdict is separate from the package status, and the difference matters to an operator:

      * a package whose documents are missing is BLOCKED - the organisation uploads a certificate
      * a package whose assembly crashed is FAILED - an engineer looks at it

    Conflating them is how an NGO is told to upload something the platform was supposed to produce,
    or an engineer is paged for a missing registration certificate.

    Nothing here says SUBMITTED. A package that is READY has NOT been sent, and the message says so.
    """
    from agent import readiness as readiness_engine

    org_id = _organisation(tenant)
    # BINDS the tenant to the session, setting app.current_org_id for row-level security. Without it
    # this handler resolves an organisation but the RLS policies see NULL and deny everything - so it
    # returns an empty list and looks like an organisation that has no packages, which is
    # indistinguishable from a correct answer. The existing contract test caught exactly that.
    require_org_access(tenant, db, org_id)

    packages = (
        db.query(models.SubmissionPackage)
        .filter(models.SubmissionPackage.org_id == org_id)
        .order_by(models.SubmissionPackage.created_at.desc())
        .limit(limit)
        .all()
    )

    items = []
    for package in packages:
        if package.opportunity_id:
            package._opportunity = db.query(models.Opportunity).filter(
                models.Opportunity.id == package.opportunity_id
            ).first()
        verdict = readiness_engine.evaluate(package)
        manifest = package.manifest or {}
        items.append({
            "package_id": str(package.id),
            "opportunity_title": manifest.get("opportunity", {}).get("title"),
            "opportunity_url": manifest.get("opportunity", {}).get("source_url"),
            "deadline": manifest.get("opportunity", {}).get("deadline"),
            "deadline_is_exact": manifest.get("opportunity", {}).get("deadline_is_exact"),
            "status": package.status,
            "readiness": verdict.verdict,
            "message": verdict.message(),
            "satisfied": verdict.satisfied,
            "required": verdict.required,
            "documents": [d.get("doc_type") for d in manifest.get("documents", [])],
            "missing": manifest.get("missing_requirements", []),
            "needs_organisation": verdict.organisation_actions,
            "version": package.application_version,
            "submission_mode": package.submission_mode,
            # Never true until a receipt exists. The model refuses SUBMITTED without one.
            "submitted": package.status == models.SubmissionPackage.SUBMITTED,
            "created_at": package.created_at.isoformat() if package.created_at else None,
        })

    ready = sum(1 for item in items if item["readiness"] == readiness_engine.READY)
    blocked = sum(1 for item in items if item["readiness"] == readiness_engine.BLOCKED)
    failed = sum(1 for item in items if item["readiness"] == readiness_engine.FAILED)
    return {
        "packages": items,
        "total": len(items),
        "ready": ready,
        "blocked": blocked,
        # Blocked is NOT failed. Reporting a missing certificate as a technical failure is the
        # distinction this whole surface exists to keep.
        "failed": failed,
        "submitted": sum(1 for item in items if item["submitted"]),
    }


@router.get("/grants", summary="Grants this organisation holds")
def list_grants(
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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



# ===========================================================================
# NOTIFICATIONS — what a person should know
# ===========================================================================
class NotificationSummary(BaseModel):
    id: str
    category: str
    severity: str
    status: str
    title: str
    body: Optional[str] = None
    action_required: bool = False
    action_url: Optional[str] = None
    repeat_count: int = 0
    created_at: Optional[str] = None
    last_raised_at: Optional[str] = None
    source_event_type: Optional[str] = None


class NotificationMarkRequest(BaseModel):
    status: str = Field(..., description="READ, ACTIONED or DISMISSED")


def _notification_service(db: Session, org_id: str, user_id: str):
    """The service for one person's inbox.

    Channels are seeded with the in-app channel only. An external channel is refused by the
    service, so a notification read over HTTP can never cause something to leave the
    platform - reading an inbox must not have side effects.
    """
    from agent.notifications.providers.fake import InAppChannel
    from agent.notifications.service import NotificationService

    return NotificationService(
        db, org_id=org_id, channels=[InAppChannel()], default_recipients=[user_id]
    )


@router.get("/notifications", summary="Your notifications, most urgent first")
def list_notifications(
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    unread_only: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Ordered by severity before recency.

    A critical notification raised yesterday matters more than an informational one raised a
    minute ago, and a chronological inbox buries it under the noise that arrived since.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    service = _notification_service(db, org_id, str(user.id))
    rows = service.inbox(
        user_id=str(user.id), unread_only=unread_only, limit=max(1, min(limit, 200))
    )
    return {
        "organisation_id": org_id,
        "count": len(rows),
        "summary": service.summary(user_id=str(user.id)),
        "notifications": [
            NotificationSummary(
                id=n.id, category=n.category, severity=n.severity, status=n.status,
                title=n.title, body=n.body, action_required=n.action_required,
                action_url=n.action_url, repeat_count=n.repeat_count or 0,
                created_at=_iso(n.created_at), last_raised_at=_iso(n.last_raised_at),
                source_event_type=n.source_event_type,
            ).model_dump()
            for n in rows
        ],
    }


@router.post("/notifications/{notification_id}", summary="Read, action or dismiss")
def mark_notification(
    notification_id: str,
    request: NotificationMarkRequest,
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Mark a notification.

    There is no DELETE. A notification that was raised is evidence the platform knew, and
    one its own subject can erase is not evidence - dismissing is the way to clear it.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    service = _notification_service(db, org_id, str(user.id))
    try:
        notification = service.mark(
            notification_id=notification_id, user_id=str(user.id), status=request.status
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    db.commit()
    return {
        "id": notification.id,
        "status": notification.status,
        "read_at": _iso(notification.read_at),
    }


@router.get("/health", summary="Fleet, relay and autonomy health")
def agent_health(
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Refuse it. Needs membership only: anyone who can see it can stop it."""
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)
    return _decide("reject", intent_id, db, org_id, user.id, payload.note)


@router.post("/mail/send-intents/{intent_id}/request-changes", summary="Ask for a different message")
def request_changes_send_intent(
    intent_id: str,
    payload: ApprovalDecisionRequest = ApprovalDecisionRequest(),
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The current intent becomes unsendable, so nobody can wave it through later."""
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)
    return _decide("request-changes", intent_id, db, org_id, user.id, payload.note)


@router.post("/mail/send-intents/{intent_id}/cancel", summary="Cancel before it leaves")
def cancel_send_intent(
    intent_id: str,
    payload: ApprovalDecisionRequest = ApprovalDecisionRequest(),
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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
    user: models.User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
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


# ---------------------------------------------------------------------------
# Reading mail
#
# THE GAP THIS FILLS. Inbound mail was being stored correctly - tenant-scoped, labelled with the
# mailbox it arrived at - and there was no way for anybody to read it. Every route in this module
# concerned OUTBOUND messages: `send-intents` and their approvals. So a code arriving at an NGO's
# portal mailbox was visible only to an engineer with psql.
#
# VERIFICATION CODES ARE REDACTED ON THE WAY OUT. A one-time code is a credential for the length of
# its window: anyone who reads it can complete - or hijack - the registration it belongs to. The
# browser worker already treats a code as a per-run secret and keeps it out of the trace; this keeps
# it out of the product surface too, so a screenshot of the inbox is not a screenshot of somebody's
# pending account. The masked form keeps the message recognisable ("a code arrived") without
# handing the value over.
# ---------------------------------------------------------------------------

#: Subject/body terms that make a message verification-shaped. Deliberately the same vocabulary the
#: extractor uses, so the redaction cannot disagree with what the platform treats as a code.
_VERIFICATION_TERMS = (
    "verification", "verify", "confirmation code", "confirm your", "one-time",
    "one time password", "otp", "security code", "activation code", "sign-in code",
)

#: A standalone run of 4-8 digits. Bounded so a phone number or a year is not mistaken for a code,
#: and word-bounded so a longer number is not partially masked into nonsense.
_CODE_PATTERN = None  # built lazily below


def _redact_verification_code(subject: Optional[str], preview: Optional[str]) -> Optional[str]:
    """Mask a one-time code in a message PREVIEW, and only there.

    Only applied when the message already reads as a verification message. Masking every four-digit
    number in every message would corrupt ordinary correspondence (an invoice total, a year, a grant
    amount) and teach nobody anything.
    """
    import re as _re

    if not preview:
        return preview
    haystack = f"{subject or ''}\n{preview}".lower()
    if not any(term in haystack for term in _VERIFICATION_TERMS):
        return preview
    return _re.sub(r"(?<!\d)(\d{4,8})(?!\d)", "[code redacted]", preview)


def _mailbox_purpose(identity: models.MailIdentity) -> str:
    """What a mailbox is for, in words a person understands.

    Portal mailboxes carry a marker in `display_name` of the form
    ``portal-registration:<host>``; the public mailbox has an organisation name. Neither is
    self-explanatory to a reader, so it is translated rather than echoed.
    """
    marker = (identity.display_name or "").strip()
    if marker.startswith("portal-registration:"):
        host = marker.split(":", 1)[1] or "a portal"
        return f"Registration mail for {host}"
    if identity.is_primary:
        return "The organisation's public address"
    if identity.identity_type == models.MailIdentity.TYPE_REPLY_ALIAS:
        return "A reply alias for one conversation"
    if identity.identity_type == models.MailIdentity.TYPE_CONNECTED:
        return "A connected external mailbox"
    return marker or "A Granada mailbox"


class StartRegistrationOut(BaseModel):
    workflow_id: str
    state: str
    opportunity_id: str
    application_id: str
    documents_workflow_id: str
    note: str


@router.post(
    "/portal/registration",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask the agent to register this organisation on the portal",
    response_model=StartRegistrationOut,
)
def start_portal_registration(
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> StartRegistrationOut:
    """Create the browser work. This is the trigger that did not exist.

    MEASURED BEFORE THIS ROUTE: `WORKFLOW_BROWSER_TASK` was defined once in `agent/workflow_engine.py`
    and created by nothing - not the API, not fleet discovery, not the application flow - so
    `browser_workflows` was 0 on an agent whose settings enabled browser execution and named a real
    UN-portal goal. The handler, the specialist registration and the worker all existed. The agent
    could browse the portal and was never asked to.

    THE SUBJECT IS AN OPPORTUNITY, AND THAT IS LOAD-BEARING. `_handle_browser_task` resolves the
    application by `(agent.org_id, opportunity_id == workflow.subject_id)` and parks with
    "no application is attached to this workflow" when it finds none. Scheduling against anything else
    would create work that is guaranteed to park, which looks identical to a broken worker.

    It does not submit anything: the handler passes `submission_authorised=False` and never consults
    the agent's opinion, because submission authority is a separate decision that a human makes.
    """
    from agent.mail.gateway import AgentWake

    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    agent = _agent_for(db, org_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this organisation has no Granada agent yet, so there is nobody to do the work",
        )

    row = (
        db.query(models.Application, models.Opportunity)
        .join(models.Opportunity, models.Opportunity.id == models.Application.opportunity_id)
        .filter(models.Application.org_id == org_id)
        .order_by(models.Application.created_at.desc())
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "no application exists for this organisation yet, so there is no portal registration "
                "to perform; the browser work is scheduled against an application's opportunity"
            ),
        )
    application, opportunity = row

    # THE DOCUMENTS COME FIRST, AND NOTHING EVER ASKED FOR THEM.
    #
    # `_handle_document_generate` is registered under the DOCUMENT specialist and was created by no
    # production code at all - measured: `documents=0` in a database with 112 applications, and every
    # submission package stuck on "missing mandatory material: cover_letter, organisation_profile".
    # Both of those are PLATFORM-GENERATED (agent/documents.py): the organisation cannot upload a
    # cover letter the software writes. So the package could never become ready, the browser task
    # could never open the portal, and the blocker was reported to the operator as their own missing
    # data. It was not.
    documents_workflow = AgentWake.schedule(
        db,
        agent=agent,
        workflow_type="document_generate",
        specialist_key="DOCUMENT",
        subject_type="OPPORTUNITY",
        subject_id=str(opportunity.id),
        # Ahead of the browser task: opening a portal with no assembled package parks, which is
        # correct but wasteful when the package is a few seconds of template work away.
        run_at=None,
        payload_ref={"application_id": str(application.id)},
    )

    workflow = AgentWake.schedule(
        db,
        agent=agent,
        workflow_type="browser_task",
        # COMPLIANCE owns both `application_assemble` and `browser_task` in agent/specialists.py;
        # naming a specialist that does not advertise this work type produces UnknownSpecialist at
        # execution time rather than here, which is why it is not a free choice.
        specialist_key="COMPLIANCE",
        subject_type="OPPORTUNITY",
        subject_id=str(opportunity.id),
        payload_ref={"portal": "unpartnerportal.org", "application_id": str(application.id)},
    )
    db.commit()

    return StartRegistrationOut(
        workflow_id=str(workflow.id),
        state=str(workflow.state),
        opportunity_id=str(opportunity.id),
        application_id=str(application.id),
        documents_workflow_id=str(documents_workflow.id),
        note=(
            "The agent will open the portal. It never submits: submission authority is a separate "
            "decision, and it will wait for a verification code if one is requested."
        ),
    )


class MailboxSummary(BaseModel):
    id: str
    address: str
    purpose: str
    is_primary: bool
    status: str
    identity_type: str
    message_count: int
    unread_capable: bool = True
    last_message_at: Optional[str] = None


class DocumentSummary(BaseModel):
    id: str
    title: str
    doc_type: str
    approval_status: str
    version: int
    is_current: bool
    scope_ref: Optional[str] = None
    created_at: Optional[str] = None
    approved_at: Optional[str] = None
    size_bytes: Optional[int] = None


@router.get("/documents", summary="Documents in the organisation's vault")
def list_documents(
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Every document and whether the organisation has approved it.

    WHY THIS EXISTS. `DocumentVault` could generate, approve, reject and check approval, and NO route
    exposed any of it. The generator wrote cover letters and organisation profiles that were then
    invisible: assembly correctly refused to freeze a package built on drafts, and the organisation had
    no way to say "yes, that is us" - so the chain stopped one step from the portal with nothing in the
    product to unblock it.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    rows = (
        db.query(models.Document)
        .filter(models.Document.org_id == org_id)
        .order_by(models.Document.created_at.desc())
        .all()
    )

    documents = [
        DocumentSummary(
            id=str(row.id),
            title=row.title or "",
            doc_type=row.doc_type or "",
            approval_status=row.approval_status or "",
            version=int(row.version or 1),
            is_current=bool(row.is_current),
            scope_ref=row.scope_ref,
            created_at=row.created_at.isoformat() if row.created_at else None,
            approved_at=row.approved_at.isoformat() if row.approved_at else None,
            size_bytes=row.size_bytes,
        )
        for row in rows
    ]

    pending = [d for d in documents if d.is_current and d.approval_status == models.Document.PENDING]
    return {
        "documents": [d.model_dump() for d in documents],
        "total": len(documents),
        "pending_approval": len(pending),
        "note": (
            "A document is a draft until a named person approves it, and assembly will not freeze a "
            "package built on drafts."
            if pending
            else "No documents are waiting for approval."
        ),
    }


@router.post("/documents/{document_id}/approve", summary="Approve THIS document")
def approve_document(
    document_id: str,
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Approve one document, in the name of the person who clicked.

    The vault refuses a superseded version or a document with no checksum: approving bytes that are no
    longer the current file, or bytes nothing hashed, is the mismatch `approved_by` exists to prevent.
    """
    from agent.organisation_memory import DocumentVault, OrganisationMemoryError

    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    allowed, reason = has_permission(
        db, org_id=org_id, user_id=user.id, permission=PROFILE_WRITE_PERMISSION
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"you may not approve this organisation's documents: {reason}",
        )

    document = (
        db.query(models.Document)
        .filter(models.Document.id == document_id, models.Document.org_id == org_id)
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="no such document in this organisation")

    try:
        DocumentVault(db, org_id).approve(document, approved_by=str(user.id))
    except OrganisationMemoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()

    return {
        "id": str(document.id),
        "approval_status": document.approval_status,
        "approved_by": document.approved_by,
        "approved_at": document.approved_at.isoformat() if document.approved_at else None,
        "note": "Approved. Assembly can now freeze a package that includes it.",
    }


@router.post("/documents/{document_id}/reject", summary="Reject THIS document")
def reject_document(
    document_id: str,
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Reject one document. It stays in the vault and is attached to nothing."""
    from agent.organisation_memory import DocumentVault, OrganisationMemoryError

    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    allowed, reason = has_permission(
        db, org_id=org_id, user_id=user.id, permission=PROFILE_WRITE_PERMISSION
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"you may not change this organisation's documents: {reason}",
        )

    document = (
        db.query(models.Document)
        .filter(models.Document.id == document_id, models.Document.org_id == org_id)
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="no such document in this organisation")

    try:
        DocumentVault(db, org_id).reject(document, rejected_by=str(user.id))
    except OrganisationMemoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()

    return {
        "id": str(document.id),
        "approval_status": document.approval_status,
        "note": "Rejected. It stays in the vault and will not be attached.",
    }


@router.get("/packages", summary="Submission packages and whether they are authorised")
def list_packages(
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Every submission package, with the state that decides whether the browser may open it.

    WHY THIS EXISTS. `SubmissionService` could freeze, authorise and hand off, and no route exposed
    any of it. A package assembled to DRAFT and the browser task then refused with
    `browser.package_not_ready` - correctly, because opening a portal on a package no person has
    authorised is exactly what the gate is for - and there was nothing in the product with which to
    authorise it. The chain stopped one click from the portal.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    rows = (
        db.query(models.SubmissionPackage)
        .filter(models.SubmissionPackage.org_id == org_id)
        .order_by(models.SubmissionPackage.created_at.desc())
        .all()
    )

    def _row(package) -> dict[str, Any]:
        return {
            "id": str(package.id),
            "application_id": package.application_id,
            "opportunity_id": package.opportunity_id,
            "status": package.status,
            "status_reason": package.status_reason,
            "fingerprint": package.package_fingerprint,
            "authorised_at": package.authorised_at.isoformat() if package.authorised_at else None,
            "authorised_by": package.authorised_by,
            "handoff_ready_at": (
                package.handoff_ready_at.isoformat() if package.handoff_ready_at else None
            ),
        }

    packages = [_row(p) for p in rows]
    return {
        "packages": packages,
        "total": len(packages),
        "awaiting_authorisation": len(
            [p for p in packages if p["status"] == models.SubmissionPackage.DRAFT]
        ),
        "note": (
            "A package is frozen at a fingerprint. Authorising it approves THAT fingerprint, and the "
            "browser will not open a portal until someone has."
        ),
    }


@router.post("/packages/{package_id}/authorise", summary="Authorise THIS package for the browser")
def authorise_package(
    package_id: str,
    note: Optional[str] = None,
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Authorise one package, in the name of the person who clicked.

    The service recalculates the fingerprint from the live row rather than accepting one from the
    caller, so a client cannot authorise one package while another is filed. The permission is
    `submission.authorise`, which is deliberately not implied by being an owner.
    """
    from agent.submission.service import SubmissionService

    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    package = (
        db.query(models.SubmissionPackage)
        .filter(
            models.SubmissionPackage.id == package_id,
            models.SubmissionPackage.org_id == org_id,
        )
        .first()
    )
    if package is None:
        raise HTTPException(status_code=404, detail="no such package in this organisation")

    try:
        agent = _agent_for(db, org_id)
        if agent is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this organisation has no Granada agent, so nothing can be filed for it",
            )
        authorised = SubmissionService(db, org_id=org_id, agent_id=str(agent.id)).authorise(
            package_id=str(package.id), user_id=str(user.id), note=note
        )
    except Exception as exc:  # noqa: BLE001 - the service's refusals are the reason a person can act on
        raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
    db.commit()

    return {
        "id": str(authorised.id),
        "status": authorised.status,
        "fingerprint": authorised.package_fingerprint,
        "authorised_at": authorised.authorised_at.isoformat() if authorised.authorised_at else None,
        "note": "Authorised. The browser task may now open the portal for this package.",
    }


class ConnectMailboxRequest(BaseModel):
    """Connect an external mailbox the organisation already owns.

    THE PASSWORD IS NEVER STORED IN A COLUMN. It goes into the encrypted credential store and only a
    reference reaches `mail_accounts.credentials_ref`, which is how the mail schema was designed - the
    table has no column that could hold a password, so the rule is enforced by shape rather than by
    discipline.
    """

    address: str = Field(min_length=3, max_length=320, description="The mailbox address, e.g. ngo@gmail.com")
    imap_host: str = Field(min_length=1, max_length=255, description="e.g. imap.gmail.com")
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_user: str = Field(min_length=1, max_length=320)
    imap_password: str = Field(min_length=1, max_length=1024, description="An app password, not the account password")
    use_ssl: bool = True
    display_name: Optional[str] = Field(default=None, max_length=200)


@router.post(
    "/mail/accounts",
    status_code=status.HTTP_201_CREATED,
    summary="Connect an existing mailbox over IMAP",
)
def connect_mailbox(
    payload: ConnectMailboxRequest,
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Register the organisation's OWN mailbox so Granada can read from it.

    WHY THIS MATTERS BEYOND CONVENIENCE. Until now the only way mail reached Granada was a Cloudflare
    Email Worker on a Granada-managed address. An organisation that already has Gmail - which is most
    of them - had no path at all, and a verification code sent to their own inbox was invisible. With
    an IMAP account registered, the sync sweep reads that inbox directly and the code is found by the
    same verification engine. It is the one route to a completed portal signup that does not depend on
    DNS or on a Cloudflare account.
    """
    import uuid as _uuid

    from agent.credential_store import store_from_settings
    from config import settings

    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    allowed, reason = has_permission(
        db, org_id=org_id, user_id=user.id, permission=PROFILE_WRITE_PERMISSION
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"you may not change this organisation's mailboxes: {reason}",
        )

    agent = _agent_for(db, org_id)
    if agent is None:
        # Named rather than guessed: a mailbox belongs to an agent, and an organisation without one
        # cannot own mail. Organisation creation provisions the agent, so this means it was lost.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this organisation has no Granada agent yet, so it cannot own a mailbox",
        )

    address = (payload.address or "").strip().lower()
    if "@" not in address:
        raise HTTPException(status_code=400, detail="the address is not an email address")

    existing = (
        db.query(models.MailAccount)
        .filter(models.MailAccount.org_id == org_id, models.MailAccount.address == address)
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{address} is already connected to this organisation",
        )

    account_id = str(_uuid.uuid4())
    ref = f"mail/imap/{account_id}"
    try:
        store = store_from_settings(db, settings)
        store.put(
            org_id=org_id,
            ref=ref,
            payload={
                "host": payload.imap_host.strip(),
                "port": int(payload.imap_port),
                "username": payload.imap_user.strip(),
                "password": payload.imap_password,
                # THE NAME MATTERS. This was written as "ssl" while the transport builder read
                # "use_ssl", so a mailbox connected with SSL off was built with SSL on and could not
                # reach its server. Both names are now written and both are read.
                "use_ssl": bool(payload.use_ssl),
                "ssl": bool(payload.use_ssl),
            },
            kind=models.CredentialSecret.KIND_IMAP_PASSWORD,
            note=f"IMAP credentials for {address}",
        )
    except Exception as exc:  # noqa: BLE001
        # A store that cannot encrypt means no credential, and a mailbox registered without one would
        # look connected and fetch nothing. Refuse instead.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"the credential store is unavailable, so nothing was saved: {type(exc).__name__}",
        ) from exc

    service = GranadaMail(db, org_id=org_id, agent_id=str(agent.id))
    account = service.add_account(
        provider="imap",
        provider_account_id=address,
        address=address,
        connection_type=models.MailAccount.CONNECTION_CONNECTED_IMAP,
        display_name=payload.display_name,
        credentials_ref=ref,
    )
    db.commit()

    return {
        "id": account.id,
        "address": account.address,
        "provider": account.provider,
        "connection_type": account.connection_type,
        "status": account.status,
        "note": (
            "The mailbox is registered. Granada reads it on the sync sweep; the password is stored "
            "encrypted and only its reference is kept on the account."
        ),
    }


class MailMessageSummary(BaseModel):
    id: str
    mailbox_id: Optional[str]
    mailbox_address: Optional[str]
    direction: str
    sender: Optional[str]
    sender_name: Optional[str]
    subject: Optional[str]
    #: REDACTED. A verification code never appears here; see `_redact_verification_code`.
    preview: Optional[str]
    received_at: Optional[str]
    created_at: Optional[str]
    processing_status: str
    has_body: bool
    #: True when the stored preview contained a code that was masked, so a client can say
    #: "a verification code arrived" without ever receiving the value.
    code_redacted: bool = False


@router.get("/mail/mailboxes", summary="The mailboxes this organisation holds")
def list_mailboxes(
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Every mailbox, with its purpose and how much mail it holds.

    An organisation holds one mailbox PER PORTAL ACCOUNT, so this is a list rather than a single
    inbox: keeping them distinguishable is the whole reason the mailbox is recorded on each message.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    identities = (
        db.query(models.MailIdentity)
        .filter(models.MailIdentity.org_id == org_id)
        .order_by(models.MailIdentity.is_primary.desc(), models.MailIdentity.created_at)
        .all()
    )

    mailboxes: list[dict[str, Any]] = []
    for identity in identities:
        rows = (
            db.query(models.MailMessage)
            .filter(
                models.MailMessage.org_id == org_id,
                models.MailMessage.mail_identity_id == identity.id,
            )
            .order_by(models.MailMessage.created_at.desc())
            .limit(1)
            .all()
        )
        count = (
            db.query(models.MailMessage)
            .filter(
                models.MailMessage.org_id == org_id,
                models.MailMessage.mail_identity_id == identity.id,
            )
            .count()
        )
        mailboxes.append(
            {
                "id": identity.id,
                "address": identity.address,
                "purpose": _mailbox_purpose(identity),
                "is_primary": bool(identity.is_primary),
                "status": identity.status,
                "identity_type": identity.identity_type,
                "message_count": count,
                "last_message_at": _iso(rows[0].created_at) if rows else None,
            }
        )

    return {
        "mailboxes": mailboxes,
        "total": len(mailboxes),
        # Named explicitly: an organisation that has provisioned no mailbox and one whose mailbox has
        # simply received nothing are different facts, and only one of them is a problem.
        "unassigned_messages": (
            db.query(models.MailMessage)
            .filter(
                models.MailMessage.org_id == org_id,
                models.MailMessage.mail_identity_id.is_(None),
            )
            .count()
        ),
    }


@router.get("/mail/messages", summary="Mail held by this organisation")
def list_messages(
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    mailbox_id: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = 50,
    before: Optional[datetime] = None,
) -> dict[str, Any]:
    """Newest first, optionally for one mailbox, cursor-paginated by `before`.

    BODIES ARE NOT RETURNED, by design. `mail_messages` stores a reference to object storage and a
    preview, because mail is among the largest and most sensitive things Granada holds; a list view
    reading fifty bodies out of the object store would be both slow and a much larger exposure than
    the list itself needs.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    query = db.query(models.MailMessage).filter(models.MailMessage.org_id == org_id)
    if mailbox_id:
        query = query.filter(models.MailMessage.mail_identity_id == mailbox_id)
    if direction:
        wanted = direction.strip().upper()
        if wanted not in (models.MailMessage.DIRECTION_INBOUND, models.MailMessage.DIRECTION_OUTBOUND):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="direction must be INBOUND or OUTBOUND",
            )
        query = query.filter(models.MailMessage.direction == wanted)
    if before is not None:
        # Cursor rather than offset: mail arrives while a person pages through it, and an offset
        # would silently repeat or skip rows as the list grows at the head.
        query = query.filter(models.MailMessage.created_at < before)

    limit = max(1, min(int(limit), 200))
    rows = query.order_by(models.MailMessage.created_at.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    # One lookup for the addresses, rather than a query per message.
    mailbox_ids = {row.mail_identity_id for row in rows if row.mail_identity_id}
    addresses: dict[str, str] = {}
    if mailbox_ids:
        for identity in (
            db.query(models.MailIdentity)
            .filter(models.MailIdentity.org_id == org_id,
                    models.MailIdentity.id.in_(mailbox_ids))
            .all()
        ):
            addresses[identity.id] = identity.address

    messages = []
    for row in rows:
        redacted = _redact_verification_code(row.subject, row.body_preview)
        messages.append(
            {
                "id": row.id,
                "mailbox_id": row.mail_identity_id,
                "mailbox_address": addresses.get(row.mail_identity_id or ""),
                "direction": row.direction,
                "sender": row.sender,
                "sender_name": row.sender_name,
                "subject": row.subject,
                "preview": redacted,
                "received_at": _iso(row.received_at),
                "created_at": _iso(row.created_at),
                "processing_status": row.processing_status,
                "has_body": bool(row.body_ref),
                "code_redacted": bool(redacted and redacted != row.body_preview),
            }
        )

    return {
        "messages": messages,
        "count": len(messages),
        "has_more": has_more,
        # The cursor for the next page, taken from the LAST row returned.
        "next_before": _iso(rows[-1].created_at) if has_more and rows else None,
    }


# ---------------------------------------------------------------------------
# Funding: what the discovery bot found, and what the matcher decided
#
# THE GAP THIS FILLS. A discovery sweep now populates a shared catalogue of 112 opportunities and the
# matcher writes 95 verdicts against them - and no route exposed either. The funding pipeline was
# running correctly and completely invisible, which is why "are the bots working?" could only be
# answered by an engineer with psql.
#
# REJECTIONS AND BLOCKERS ARE SHOWN, NOT HIDDEN. `opportunity_matches` keeps `REJECTED_BY_RULE` rows
# on purpose: an organisation that cannot see what it was ruled out of, and on what basis, cannot
# correct its own profile. A screen that showed only the good matches would look tidier and be less
# useful, so this route returns the gate evidence alongside the verdict.
# ---------------------------------------------------------------------------

#: Why a gate could not be evaluated, in the organisation's own terms, and the facts that would fix
#: it. Derived from GATE_FACTS so the screen cannot drift from the matcher.
def _blocking_requirements(unknown_gates: Optional[dict]) -> list[dict[str, Any]]:
    """What the organisation would have to supply for each undecidable gate.

    This is the join between the funding screen and the profile screen: "95 matches are waiting on
    your country" is actionable, where "95 NEEDS_DATA" is not.
    """
    gates = []
    if isinstance(unknown_gates, dict):
        raw = unknown_gates.get("gates")
        if isinstance(raw, list):
            gates = [str(g) for g in raw if g]
    out = []
    for gate in gates:
        needs = list(GATE_FACTS.get(gate, ()))
        out.append(
            {
                "gate": gate,
                "needs_facts": needs,
                "action": (
                    f"Supply {', '.join(needs)} in your organisation profile"
                    if needs
                    else "This gate needs information Granada does not yet have"
                ),
            }
        )
    return out


@router.get("/opportunities", summary="Funding the matcher has considered for this organisation")
def list_opportunities(
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
    state: Optional[str] = None,
    include_inactive: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """The catalogue, left-joined with THIS organisation's verdict on each opportunity.

    LEFT JOIN, not inner: an opportunity the matcher has not reached yet is a real and interesting
    state ("the bot found it, the matcher hasn't judged it"). An inner join would hide it and make the
    catalogue look smaller than it is.

    INACTIVE LISTINGS ARE EXCLUDED BY DEFAULT, and that is a correction rather than a filter.
    Measured on production: the catalogue held 23 ReliefWeb news reports, 20 training courses and 51
    browser-test fixtures alongside 18 real funding calls. The stored verdicts for those were
    computed while they were still active, so the screen ranked a `LIVE TEST:` row at the top as
    "needs data" - a stale verdict presented as an actionable one. A person browsing funding wants
    funding they can apply to, so inactive listings are opt-in and, when included, are marked
    `stale` rather than silently mixed in.

    The catalogue is SHARED and not tenant-owned (ADR-0009); only the match is per-organisation. The
    join is therefore scoped on `org_id` so one organisation's verdicts can never appear beside
    another's - and the outer join means an unmatched opportunity still appears once.
    """
    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    # MATCHED first, then blocked, then ruled out, then not-yet-judged, then inactive. An operator's
    # attention belongs at the top of that list, not in the order rows happened to be inserted.
    priority = case(
        (models.OpportunityMatch.state == models.OpportunityMatch.MATCHED, 0),
        (models.OpportunityMatch.state == models.OpportunityMatch.NEEDS_DATA, 1),
        (models.OpportunityMatch.state == models.OpportunityMatch.REJECTED_BY_RULE, 2),
        (models.OpportunityMatch.id.is_(None), 3),
        else_=4,
    )

    query = (
        db.query(models.Opportunity, models.OpportunityMatch)
        .outerjoin(
            models.OpportunityMatch,
            and_(
                models.OpportunityMatch.opportunity_id == models.Opportunity.id,
                models.OpportunityMatch.org_id == org_id,
            ),
        )
    )
    if not include_inactive:
        query = query.filter(models.Opportunity.is_active.is_(True))
    if state:
        wanted = state.strip().upper()
        allowed = {
            models.OpportunityMatch.MATCHED,
            models.OpportunityMatch.REJECTED_BY_RULE,
            models.OpportunityMatch.NEEDS_DATA,
            models.OpportunityMatch.SUPERSEDED,
        }
        if wanted not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"state must be one of {sorted(allowed)}",
            )
        query = query.filter(models.OpportunityMatch.state == wanted)

    total = query.count()
    rows = (
        query.order_by(
            priority.asc(),
            models.OpportunityMatch.rank.asc().nulls_last(),
            models.OpportunityMatch.final_score.desc().nulls_last(),
            models.Opportunity.deadline.asc().nulls_last(),
        )
        .limit(limit)
        .offset(offset)
        .all()
    )

    items = []
    for opportunity, match in rows:
        items.append(
            {
                "id": opportunity.id,
                "title": opportunity.title,
                "source_name": opportunity.source_name,
                "source_url": opportunity.source_url,
                "country": opportunity.country,
                "deadline": _iso(opportunity.deadline),
                "amount_min": opportunity.amount_min,
                "amount_max": opportunity.amount_max,
                "currency": opportunity.currency,
                "sector": opportunity.sector,
                "is_active": opportunity.is_active,
                "match": None
                if match is None
                else {
                    "state": match.state,
                    "hard_gate_passed": bool(match.hard_gate_passed),
                    "final_score": match.final_score,
                    "rank": match.rank,
                    "computed_at": _iso(match.computed_at),
                    # The verdict was computed while the listing was live. Saying so is the
                    # difference between a stale answer and a wrong one.
                    "stale": not bool(opportunity.is_active),
                    # The evidence, not just the verdict.
                    "failed_gates": (match.failed_gates or {}).get("gates", []),
                    "unknown_gates": (match.unknown_gates or {}).get("gates", []),
                    "blocking": _blocking_requirements(match.unknown_gates),
                    "gate_reasons": [
                        {
                            "gate": gate.get("gate"),
                            "outcome": gate.get("outcome"),
                            "reason": gate.get("reason"),
                        }
                        for gate in ((match.reasons or {}).get("gates") or [])
                        if isinstance(gate, dict)
                    ],
                },
            }
        )

    # Counts for the whole catalogue, not the page: a summary that described one page would be
    # mistaken for the portfolio. Scoped to the same listings the list is scoped to, so "95 need
    # data" cannot refer to rows that are not on the screen.
    tally = {"matched": 0, "needs_data": 0, "rejected": 0, "not_evaluated": 0}
    counts_query = (
        db.query(models.OpportunityMatch.state, func.count())
        .join(models.Opportunity, models.Opportunity.id == models.OpportunityMatch.opportunity_id)
        .filter(models.OpportunityMatch.org_id == org_id)
    )
    if not include_inactive:
        counts_query = counts_query.filter(models.Opportunity.is_active.is_(True))
    for state_name, count in counts_query.group_by(models.OpportunityMatch.state).all():
        if state_name == models.OpportunityMatch.MATCHED:
            tally["matched"] = count
        elif state_name == models.OpportunityMatch.NEEDS_DATA:
            tally["needs_data"] = count
        elif state_name == models.OpportunityMatch.REJECTED_BY_RULE:
            tally["rejected"] = count

    catalogue_query = db.query(models.Opportunity)
    if not include_inactive:
        catalogue_query = catalogue_query.filter(models.Opportunity.is_active.is_(True))
    catalogue = catalogue_query.count()
    tally["not_evaluated"] = max(0, catalogue - sum(tally.values()))

    # The single most useful sentence on the screen: what would unblock the most matches.
    blocked_facts: dict[str, int] = {}
    blocked_query = (
        db.query(models.OpportunityMatch)
        .join(models.Opportunity, models.Opportunity.id == models.OpportunityMatch.opportunity_id)
        .filter(
            models.OpportunityMatch.org_id == org_id,
            models.OpportunityMatch.state == models.OpportunityMatch.NEEDS_DATA,
        )
    )
    if not include_inactive:
        blocked_query = blocked_query.filter(models.Opportunity.is_active.is_(True))
    for match in blocked_query.limit(500).all():
        for requirement in _blocking_requirements(match.unknown_gates):
            for fact in requirement["needs_facts"]:
                blocked_facts[fact] = blocked_facts.get(fact, 0) + 1

    return {
        "opportunities": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "include_inactive": bool(include_inactive),
        "summary": tally,
        "catalogue_size": catalogue,
        "blocked_by_fact": blocked_facts,
        "next_offset": offset + len(items) if offset + len(items) < total else None,
    }


# ---------------------------------------------------------------------------
# The organisation profile - the Digital Twin, and the only way to fill it
#
# WHY THIS EXISTS. `OrganisationMemory.record_fact` was reachable only from tests and from other
# services; there was no route an organisation could use to state its own country or registration.
# Measured on production: `org_facts` was EMPTY, and every one of 95 opportunity matches sat at
# NEEDS_DATA because the `country_eligible` and `registration_valid` gates could not be evaluated.
# A platform whose matching engine is correct and whose profile has no intake reports "we cannot
# tell" forever, and the operator reads that as a broken matcher.
#
# THE STATE IS NOT THE CALLER'S CHOICE. A fact written here is USER_PROVIDED with the acting user as
# its source. This route will never write VERIFIED (which means a human with authority confirmed it)
# and can never write AI_INFERRED (which is not submission-safe at all). Letting a request body name
# its own provenance would defeat the one property the table exists to guarantee.
# ---------------------------------------------------------------------------

#: Permission required to state facts about the organisation. Not `mail.approve_send`: deciding what
#: the organisation IS and deciding what it SAYS are different authorities, and an organisation may
#: reasonably grant the first more widely than the second.
PROFILE_WRITE_PERMISSION = "organisation.profile_write"

#: The facts the matching gates read, and therefore the ones that turn a NEEDS_DATA match into a
#: decidable one. Kept aligned with `agent/matching.py` by the contract test in
#: `tests/test_profile_api.py` rather than by a comment, because a gate that starts reading a new key
#: would otherwise silently keep every match parked.
GATE_FACTS: dict[str, tuple[str, ...]] = {
    "country_eligible": ("country",),
    "registration_valid": ("registration_valid_until", "registration_status"),
}

#: Facts a person is most likely to supply, with the shape each should take. Documentation for the
#: client and a sanity check for the operator - NOT a whitelist, because organisations legitimately
#: hold facts this list has never heard of (a tax exemption date, a safeguarding lead) and refusing
#: them would be the platform deciding what an organisation is allowed to know about itself.
KNOWN_FACT_KEYS: dict[str, str] = {
    "legal_name": "The registered legal name.",
    "country": "Country of registration, e.g. 'Uganda'. Read by the country_eligible gate.",
    "organisation_type": "NGO, charity, foundation, social enterprise, ...",
    "registration_number": "The charity or company registration number.",
    "registration_status": "ACTIVE, SUSPENDED, ... Read by the registration_valid gate.",
    "registration_valid_until": "ISO-8601 date the registration expires. Read by registration_valid.",
    "tax_exemption_valid_until": "ISO-8601 date a tax-exempt status expires.",
    "founding_year": "Year the organisation was founded.",
    "address": "Registered address.",
    "contact_email": "The public contact address.",
    "website": "The organisation's website.",
    "minimum_grant_amount": "The smallest award worth applying for, as an integer.",
    "annual_budget": "Most recent annual budget, as an integer.",
}


class FactInput(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    value: Any = Field(description="The fact's value. Strings are the norm; integers are accepted.")
    valid_until: Optional[datetime] = Field(
        default=None,
        description="When the fact stops being trusted. A lapsed certificate must not read as current.",
    )


class FactsWriteRequest(BaseModel):
    facts: list[FactInput] = Field(min_length=1, max_length=100)


def _fact_payload(fact: models.OrgFact) -> dict[str, Any]:
    value = fact.value
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    return {
        "key": fact.key,
        "value": value,
        "state": fact.state,
        "source": fact.source,
        "version": fact.version,
        "valid_until": _iso(fact.valid_until),
        "created_at": _iso(fact.created_at),
        # Whether this fact could actually be used in a submission. Computed rather than shown as a
        # raw state, because "USER_PROVIDED" means nothing to the person filling the form.
        "submission_usable": fact.state
        in {
            models.OrgFact.VERIFIED,
            models.OrgFact.USER_PROVIDED,
            models.OrgFact.IMPORTED,
        },
    }


def _gate_readiness(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Which matching gates the profile can currently decide, and what each one still needs.

    This is the answer to "why are my matches not actionable", produced from the same keys the
    matcher reads. Without it an operator sees a wall of NEEDS_DATA and has no way to find out that
    two short fields are the whole cause.
    """
    readiness = []
    for gate, keys in GATE_FACTS.items():
        # The matcher treats registration_valid as satisfied by EITHER the expiry or the status, so
        # one of them is enough. Requiring both here would send a person to fill a field the matcher
        # is not waiting for.
        present = [key for key in keys if facts.get(key) not in (None, "")]
        readiness.append(
            {
                "gate": gate,
                "decidable": bool(present),
                "needs": [key for key in keys if key not in present],
                "satisfied_by": present,
            }
        )
    return readiness


@router.get("/profile/facts", summary="The organisation's profile facts")
def list_profile_facts(
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Every current fact, plus which matching gates remain undecidable without them.

    Readable by any member of the organisation: knowing what the platform believes about the
    organisation is not a privileged act, and a person cannot correct a wrong fact they cannot see.
    """
    from agent.organisation_memory import OrganisationMemory

    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    memory = OrganisationMemory(db, org_id)
    current = memory.current_facts()
    usable = memory.submission_facts()

    return {
        "facts": [_fact_payload(fact) for fact in current],
        "total": len(current),
        # What the matcher can use, as opposed to what is merely stored.
        "usable_fact_keys": sorted(usable),
        "matching_gates": _gate_readiness(usable),
        # Stated plainly, because this is the single fact that explains a NEEDS_DATA catalogue.
        "blocks_matching": sorted(
            gate["gate"] for gate in _gate_readiness(usable) if not gate["decidable"]
        ),
        "suggested_keys": KNOWN_FACT_KEYS,
    }


@router.put("/profile/facts", summary="State facts about the organisation")
def write_profile_facts(
    payload: FactsWriteRequest,
    user: Any = Depends(get_current_user),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record one or more facts as USER_PROVIDED, superseding any earlier version.

    A fact is NEVER overwritten in place. `record_fact` appends a new version and supersedes the
    current one, so an application assembled against version 3 stays explainable after version 4
    arrives - which is the difference between a platform that can answer "why did you say that?"
    and one that cannot.

    Partial success is deliberate. If one fact in the batch is unusable the others are still
    recorded and the problem is reported by key: refusing the whole batch would make an operator
    re-enter twenty good answers to fix one bad one.
    """
    from agent.organisation_memory import OrganisationMemory, OrganisationMemoryError, UnknownFactState

    org_id = _organisation(tenant)
    require_org_access(tenant, db, org_id)

    allowed, reason = has_permission(
        db, org_id=org_id, user_id=user.id, permission=PROFILE_WRITE_PERMISSION
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"you may not change this organisation's profile: {reason}",
        )

    memory = OrganisationMemory(db, org_id, actor_id=str(user.id))
    recorded: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for fact in payload.facts:
        key = (fact.key or "").strip()
        if not key:
            rejected.append({"key": fact.key, "reason": "a fact key is required"})
            continue
        if fact.value is None or (isinstance(fact.value, str) and not fact.value.strip()):
            # An empty value is not a fact. Storing "" would let a gate treat "blank" as "known" and
            # produce a match the organisation cannot actually support.
            rejected.append({"key": key, "reason": "an empty value is not a fact"})
            continue
        try:
            row = memory.record_fact(
                key=key,
                value=fact.value,
                # FIXED, never taken from the request: see the section comment.
                state=models.OrgFact.USER_PROVIDED,
                source=f"user:{user.id}",
                value_type="date" if fact.valid_until is not None else "text",
                valid_until=fact.valid_until,
            )
        except (OrganisationMemoryError, UnknownFactState) as exc:
            rejected.append({"key": key, "reason": str(exc)[:200]})
            continue
        recorded.append({"key": key, "version": row.version, "state": row.state})

    db.commit()

    # Re-read after the commit so the answer describes what is durable rather than what was staged.
    usable = OrganisationMemory(db, org_id).submission_facts()
    return {
        "recorded": recorded,
        "rejected": rejected,
        "usable_fact_keys": sorted(usable),
        "matching_gates": _gate_readiness(usable),
        "blocks_matching": sorted(
            gate["gate"] for gate in _gate_readiness(usable) if not gate["decidable"]
        ),
    }
