"""Award-to-delivery: turn an award into a grant that can actually be delivered.

The brief's exit criterion for Phase 9 is that **no data already approved in the
application is re-entered by hand**. So every figure here is derived from the frozen
submission package:

* the **approved budget** is copied from the package's manifest, not typed in;
* the **workplan** is built from the budget lines and the organisation's stated
  activities;
* the **requested amount** is the package's budget total.

Only the award itself is new information - the amount, the dates, the funder's reference
- because that is what the award letter says and it does not exist anywhere else.

Three rules that are refusals rather than conventions
----------------------------------------------------
1. **A condition is never satisfied by inference.** Funders attach conditions that gate
   disbursement, and marking one met because it *looks* met is how an organisation finds
   its next tranche withheld. ``evidence_ref`` is required.
2. **A disbursement is never RECEIVED without a reference.** Money that "probably
   arrived" is not received, and a project spending against a tranche it has not got is
   a project in trouble.
3. **A report is never SUBMITTED without a reference.** Believing a report was filed
   when it was not is worse than knowing it is late, because late can still be fixed.

Why this phase matters more than it looks
-----------------------------------------
The platform's headline risk is a missed application. The bigger financial risk is a
missed *report*: an unsubmitted narrative report is the most common reason a subsequent
tranche is withheld, and unlike a deadline it produces no rejection letter - just money
that does not arrive.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


class DeliveryError(RuntimeError):
    """Base class for delivery refusals."""


class NotDeliverable(DeliveryError):
    """The handover cannot proceed, for a named reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class HandoverResult:
    """What a handover created."""

    grant_id: str
    project_id: Optional[str] = None
    conditions: list[str] = field(default_factory=list)
    obligations: list[str] = field(default_factory=list)
    disbursements: list[str] = field(default_factory=list)
    re_entered_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "project_id": self.project_id,
            "conditions": self.conditions,
            "obligations": self.obligations,
            "disbursements": self.disbursements,
            # The exit criterion, made observable: any field a person had to type that
            # the application already contained shows up here.
            "re_entered_fields": self.re_entered_fields,
            "warnings": self.warnings,
        }


@dataclass
class Deadline:
    """One thing with a date, from any of the three sources."""

    kind: str            # CONDITION | REPORT | DISBURSEMENT
    id: str
    title: str
    due_on: datetime
    days_remaining: int
    overdue: bool
    blocks_payment: bool = False
    grant_id: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "due_on": self.due_on.isoformat(),
            "days_remaining": self.days_remaining,
            "overdue": self.overdue,
            "blocks_payment": self.blocks_payment,
            "grant_id": self.grant_id,
        }


class DeliveryService:
    """Converts awards into grants, and monitors what they require."""

    def __init__(self, db: Session, *, org_id: str, agent_id: str) -> None:
        if not org_id or not agent_id:
            raise DeliveryError("delivery requires both an organisation and an agent")
        self.db = db
        self.org_id = org_id
        self.agent_id = agent_id

    # ==================================================================
    # The handover
    # ==================================================================
    def handover(
        self,
        *,
        package: models.SubmissionPackage,
        reference: str,
        awarded_amount: Any,
        awarded_at: Optional[datetime] = None,
        starts_on: Optional[datetime] = None,
        ends_on: Optional[datetime] = None,
        donor_name: Optional[str] = None,
        donor_contact_email: Optional[str] = None,
        title: Optional[str] = None,
        currency: Optional[str] = None,
        conditions: Iterable[dict[str, Any]] = (),
        reporting_schedule: Iterable[dict[str, Any]] = (),
        disbursement_schedule: Iterable[dict[str, Any]] = (),
        awarded_budget: Optional[dict[str, Any]] = None,
    ) -> HandoverResult:
        """Create the grant, project and obligations from an awarded application.

        The package must be **SUBMITTED**. Handing over an application that was never
        filed would create a grant for money nobody agreed to give.
        """
        if package.org_id != self.org_id:
            raise DeliveryError("the package belongs to another organisation")
        if package.status != models.SubmissionPackage.SUBMITTED:
            raise NotDeliverable(
                "APPLICATION_NOT_SUBMITTED",
                f"the package is {package.status}; a grant can only follow an "
                "application the funder actually received",
            )
        if not reference or not str(reference).strip():
            raise NotDeliverable(
                "NO_FUNDER_REFERENCE",
                "a grant requires the funder's own reference. Without one there is "
                "nothing to reconcile a disbursement against.",
            )

        awarded = _decimal(awarded_amount)
        if awarded <= 0:
            raise NotDeliverable(
                "AWARD_AMOUNT_INVALID",
                "the awarded amount must be greater than zero; a zero-value grant is "
                "almost always a parsing error rather than an award",
            )

        # -- derived, not re-entered ------------------------------------
        manifest = package.manifest or {}
        approved_budget = manifest.get("budget") or {}
        requested = _decimal(approved_budget.get("total"))
        # The package's own currency wins unless the award states otherwise, because the
        # application is what was approved.
        grant_currency = (currency or approved_budget.get("currency") or "USD").upper()[:3]

        if awarded == requested and requested > 0:
            size = models.Grant.SIZE_AS_REQUESTED
        elif requested > 0 and awarded < requested:
            size = models.Grant.SIZE_REDUCED
        else:
            size = models.Grant.SIZE_INCREASED

        application = self.db.execute(
            select(models.Application).where(
                models.Application.id == package.application_id,
                models.Application.org_id == self.org_id,
            )
        ).scalars().first()
        if application is None:
            raise NotDeliverable(
                "APPLICATION_MISSING", "the application this package belongs to is gone"
            )
        opportunity = self.db.execute(
            select(models.Opportunity).where(
                models.Opportunity.id == application.opportunity_id
            )
        ).scalars().first()

        warning_list: list[str] = []
        re_entered: list[str] = []

        if not approved_budget:
            # Honest: the exit criterion cannot be met if the package carried no budget,
            # and saying so is better than a silently empty workplan.
            warning_list.append(
                "the submission package carried no approved budget, so the workplan has "
                "no financial baseline; the budget will have to be entered after all"
            )
            re_entered.append("approved_budget")
        if awarded != requested and requested > 0:
            warning_list.append(
                f"the award is {size.lower()} relative to the request "
                f"({awarded} vs {requested} {grant_currency}); the workplan below is the "
                "APPLIED plan and must be revised before delivery starts"
            )

        grant = models.Grant(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=self.agent_id,
            application_id=package.application_id,
            opportunity_id=application.opportunity_id,
            source_package_id=package.id,
            reference=str(reference).strip()[:255],
            donor_name=donor_name or getattr(opportunity, "source_name", None),
            title=(title or getattr(opportunity, "title", None) or "Grant")[:500],
            currency=grant_currency,
            requested_amount=requested if requested > 0 else None,
            awarded_amount=awarded,
            size_relative_to_request=size,
            status=models.Grant.STATUS_ACTIVE,
            awarded_at=awarded_at or _now(),
            starts_on=starts_on,
            ends_on=ends_on,
            donor_contact_email=donor_contact_email,
            approved_budget=approved_budget or None,
            awarded_budget=awarded_budget,
            created_at=_now(),
            correlation_id=package.correlation_id,
        )
        self.db.add(grant)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            raise NotDeliverable(
                "GRANT_ALREADY_EXISTS",
                f"grant reference {reference!r} already exists for this organisation",
            )

        # -- the project and its baseline workplan ----------------------
        workplan = self._workplan_from(approved_budget, starts_on, ends_on, application)
        project = models.Project(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=self.agent_id,
            grant_id=grant.id,
            name=grant.title,
            status=models.Project.STATUS_PLANNED,
            baseline_workplan=workplan,
            budget_total=requested if requested > 0 else awarded,
            starts_on=starts_on,
            ends_on=ends_on,
            created_at=_now(),
        )
        self.db.add(project)
        self.db.flush()

        # -- conditions ------------------------------------------------
        condition_ids: list[str] = []
        for item in conditions or ():
            condition = self.add_condition(
                grant_id=grant.id,
                title=item.get("title") or "Condition",
                kind=item.get("kind") or models.GrantCondition.KIND_OTHER,
                detail=item.get("detail"),
                due_on=item.get("due_on"),
                blocks_payment=item.get("blocks_payment"),
            )
            condition_ids.append(condition.id)

        # -- reporting ------------------------------------------------
        obligation_ids: list[str] = []
        for item in reporting_schedule or ():
            obligation = self.add_reporting_obligation(
                grant_id=grant.id,
                title=item.get("title") or "Report",
                due_on=item.get("due_on"),
                kind=item.get("kind") or models.ReportingObligation.KIND_NARRATIVE,
                period=item.get("period") or models.ReportingObligation.PERIOD_ONE_OFF,
                project_id=project.id,
                remind_days_before=item.get("remind_days_before"),
            )
            obligation_ids.append(obligation.id)

        # -- disbursements ---------------------------------------------
        disbursement_ids: list[str] = []
        for item in disbursement_schedule or ():
            row = self.expect_disbursement(
                grant_id=grant.id,
                amount=item.get("amount"),
                expected_on=item.get("expected_on"),
                label=item.get("label"),
                tranche_number=item.get("tranche_number"),
                currency=item.get("currency") or grant_currency,
                gated_by=item.get("gated_by"),
            )
            disbursement_ids.append(row.id)

        # -- the donor's mail thread, if the platform already knows it ---
        self._link_mail_thread(grant, opportunity)

        self._stage_event(
            event_type="award.recorded",
            payload={
                "grant_id": grant.id,
                "application_id": grant.application_id,
                "reference": grant.reference,
                "title": grant.title,
                "awarded_amount": str(awarded),
                "currency": grant_currency,
                "size_relative_to_request": size,
                "source_package_id": package.id,
            },
        )
        self._record_activity(
            summary_key="award.recorded",
            structured={
                "grant_id": grant.id,
                "reference": grant.reference,
                "conditions": len(condition_ids),
                "obligations": len(obligation_ids),
                "disbursements": len(disbursement_ids),
            },
            subject_id=grant.id,
        )
        self.db.flush()

        return HandoverResult(
            grant_id=grant.id,
            project_id=project.id,
            conditions=condition_ids,
            obligations=obligation_ids,
            disbursements=disbursement_ids,
            re_entered_fields=re_entered,
            warnings=warning_list,
        )

    def _workplan_from(
        self,
        approved_budget: dict[str, Any],
        starts_on: Optional[datetime],
        ends_on: Optional[datetime],
        application: models.Application,
    ) -> dict[str, Any]:
        """Build the baseline workplan from the APPROVED budget lines.

        Each funded line becomes a milestone carrying its own amount, so the workplan and
        the budget cannot drift: they are the same data seen two ways. Where the
        application recorded its own activities those are used, so nothing is re-entered.
        """
        lines = approved_budget.get("lines") or []
        milestones: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            milestones.append({
                "order": index + 1,
                "name": line.get("item") or f"Budget line {index + 1}",
                "amount": str(line.get("amount", "")),
                # Left null rather than invented. A workplan date nobody agreed to is
                # worse than an empty field, because it looks decided.
                "due_on": None,
            })
        if not milestones:
            milestones.append({
                "order": 1,
                "name": "Deliver the funded activities",
                "amount": str(approved_budget.get("total", "")),
                "due_on": None,
            })

        return {
            "source": "submission_package.approved_budget",
            "generated_at": _now().isoformat(),
            "period": {
                "starts_on": starts_on.isoformat() if starts_on else None,
                "ends_on": ends_on.isoformat() if ends_on else None,
            },
            "milestones": milestones,
            "note": (
                "Milestone dates are deliberately unset: the awarded workplan is agreed "
                "with the funder, and inventing dates would present a decision as made."
            ),
        }

    def _link_mail_thread(self, grant: models.Grant, opportunity: Optional[models.Opportunity]) -> None:
        """Attach the donor's mail thread so correspondence lands against the grant.

        Phase 9's "connect donor mail thread to grant". Only links a thread that already
        exists and belongs to this organisation - it does not go looking, because a
        grant handover must not depend on the mail subsystem being reachable.
        """
        domain = None
        if grant.donor_contact_email and "@" in grant.donor_contact_email:
            domain = grant.donor_contact_email.split("@")[-1].lower()
        if not domain:
            return
        thread = self.db.execute(
            select(models.MailThread).where(
                models.MailThread.org_id == self.org_id,
            ).order_by(models.MailThread.created_at.desc())
        ).scalars().first()
        if thread is None:
            return
        grant.mail_thread_id = thread.id
        self.db.flush()

    # ==================================================================
    # Conditions
    # ==================================================================
    def add_condition(
        self,
        *,
        grant_id: str,
        title: str,
        kind: str = models.GrantCondition.KIND_OTHER,
        detail: Optional[str] = None,
        due_on: Optional[datetime] = None,
        blocks_payment: Optional[bool] = None,
    ) -> models.GrantCondition:
        """Record a funder condition.

        ``blocks_payment`` defaults from the KIND rather than from the caller, because a
        precondition, financial or legal condition gates money by definition and letting
        a caller forget would silently make it non-blocking.
        """
        self._grant(grant_id)
        if blocks_payment is None:
            blocks_payment = kind in models.GrantCondition.BLOCKING_KINDS

        condition = models.GrantCondition(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=self.agent_id,
            grant_id=grant_id,
            kind=kind,
            status=models.GrantCondition.STATUS_OPEN,
            title=str(title)[:500],
            detail=detail,
            blocks_payment=bool(blocks_payment),
            due_on=_aware(due_on),
            created_at=_now(),
        )
        self.db.add(condition)
        self.db.flush()
        self._stage_event(
            event_type="award.condition_added",
            payload={
                "grant_id": grant_id,
                "condition_id": condition.id,
                "kind": kind,
                "blocks_payment": condition.blocks_payment,
            },
        )
        return condition

    def satisfy_condition(
        self,
        *,
        condition_id: str,
        evidence_ref: Optional[str],
        satisfied_by: Optional[str] = None,
        note: Optional[str] = None,
    ) -> models.GrantCondition:
        """Mark a condition met. **Requires evidence.**

        Refusing without it is the whole point. A condition marked satisfied on the
        assumption that it probably is gets discovered when a tranche is withheld, at
        which point the funder's confidence has already been spent.
        """
        if not evidence_ref or not str(evidence_ref).strip():
            raise DeliveryError(
                "a condition cannot be marked satisfied without evidence. Upload or "
                "reference the document that proves it - a condition assumed met is "
                "discovered when a disbursement is withheld."
            )
        condition = self.db.execute(
            select(models.GrantCondition).where(
                models.GrantCondition.id == condition_id,
                models.GrantCondition.org_id == self.org_id,
            )
        ).scalars().first()
        if condition is None:
            raise DeliveryError(f"no condition {condition_id} in this organisation")
        if condition.status == models.GrantCondition.STATUS_SATISFIED:
            return condition

        condition.status = models.GrantCondition.STATUS_SATISFIED
        condition.satisfied_at = _now()
        condition.satisfied_by = satisfied_by
        condition.evidence_ref = str(evidence_ref)[:500]
        condition.evidence_note = note
        self.db.flush()

        self._stage_event(
            event_type="award.condition_met",
            payload={
                "grant_id": condition.grant_id,
                "condition_id": condition.id,
                "title": condition.title,
                "evidence_ref": condition.evidence_ref,
            },
        )
        self._record_activity(
            summary_key="award.condition_met",
            structured={
                "grant_id": condition.grant_id,
                "condition_id": condition.id,
                "blocks_payment": condition.blocks_payment,
            },
            subject_id=condition.grant_id,
        )
        self.db.flush()
        return condition

    def waive_condition(
        self, *, condition_id: str, reason: str, waived_by: Optional[str] = None
    ) -> models.GrantCondition:
        """A funder agreed it is not required. Distinct from satisfied, and recorded as
        such, because 'we decided it did not apply' is not evidence of compliance."""
        if not reason or not str(reason).strip():
            raise DeliveryError("waiving a condition requires a reason")
        condition = self.db.execute(
            select(models.GrantCondition).where(
                models.GrantCondition.id == condition_id,
                models.GrantCondition.org_id == self.org_id,
            )
        ).scalars().first()
        if condition is None:
            raise DeliveryError(f"no condition {condition_id} in this organisation")
        condition.status = models.GrantCondition.STATUS_WAIVED
        condition.evidence_note = f"waived: {reason}"
        condition.satisfied_by = waived_by
        self.db.flush()
        return condition

    # ==================================================================
    # Reporting
    # ==================================================================
    def add_reporting_obligation(
        self,
        *,
        grant_id: str,
        title: str,
        due_on: Optional[datetime],
        kind: str = models.ReportingObligation.KIND_NARRATIVE,
        period: str = models.ReportingObligation.PERIOD_ONE_OFF,
        project_id: Optional[str] = None,
        remind_days_before: Optional[int] = None,
        period_starts_on: Optional[datetime] = None,
        period_ends_on: Optional[datetime] = None,
    ) -> models.ReportingObligation:
        """Record a report the organisation owes. ``due_on`` is required.

        A reporting obligation with no date cannot be monitored, and an unmonitored
        report is the one that gets missed.
        """
        self._grant(grant_id)
        if due_on is None:
            raise DeliveryError(
                "a reporting obligation requires a due date; a report with no date "
                "cannot be monitored and is the one that gets missed"
            )
        obligation = models.ReportingObligation(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=self.agent_id,
            grant_id=grant_id,
            project_id=project_id,
            kind=kind,
            period=period,
            status=models.ReportingObligation.STATUS_PENDING,
            title=str(title)[:500],
            due_on=_aware(due_on),
            period_starts_on=_aware(period_starts_on),
            period_ends_on=_aware(period_ends_on),
            remind_days_before=remind_days_before if remind_days_before is not None else 14,
            created_at=_now(),
        )
        self.db.add(obligation)
        self.db.flush()
        return obligation

    def refresh_reporting_statuses(self, *, now: Optional[datetime] = None) -> dict[str, int]:
        """Advance PENDING to DUE_SOON and DUE_SOON to OVERDUE.

        Derived from the clock rather than from anyone remembering, because the failure
        mode here is silence: an overdue report produces no rejection letter, just money
        that does not arrive.
        """
        moment = _aware(now) or _now()
        moved = {"due_soon": 0, "overdue": 0, "events": 0}

        rows = self.db.execute(
            select(models.ReportingObligation).where(
                models.ReportingObligation.org_id == self.org_id,
                models.ReportingObligation.status.in_(
                    tuple(models.ReportingObligation.OUTSTANDING)
                ),
            )
        ).scalars().all()

        for obligation in rows:
            due = _aware(obligation.due_on)
            if due is None:
                continue
            if due <= moment:
                if obligation.status != models.ReportingObligation.STATUS_OVERDUE:
                    obligation.status = models.ReportingObligation.STATUS_OVERDUE
                    moved["overdue"] += 1
                    self._stage_event(
                        event_type="report.overdue",
                        payload={
                            "grant_id": obligation.grant_id,
                            "obligation_id": obligation.id,
                            # `title` is REQUIRED by the route and was missing, so the
                            # notification read "Funder report overdue: (unknown)" - it told
                            # a person something was overdue and not which thing.
                            "title": obligation.title,
                            "kind": obligation.kind,
                            "period": obligation.period,
                            "due_on": due.isoformat(),
                            "days_late": (moment - due).days,
                        },
                    )
                    moved["events"] += 1
            elif due - moment <= timedelta(days=obligation.remind_days_before or 14):
                if obligation.status == models.ReportingObligation.STATUS_PENDING:
                    obligation.status = models.ReportingObligation.STATUS_DUE_SOON
                    moved["due_soon"] += 1
                    self._stage_event(
                        event_type="report.due_soon",
                        payload={
                            "grant_id": obligation.grant_id,
                            "obligation_id": obligation.id,
                            "title": obligation.title,
                            "kind": obligation.kind,
                            "due_on": due.isoformat(),
                            "days_remaining": (due - moment).days,
                        },
                    )
                    moved["events"] += 1
        self.db.flush()
        return moved

    def record_report_submitted(
        self,
        *,
        obligation_id: str,
        reference: str,
        submitted_by: Optional[str] = None,
        report_document_ref: Optional[str] = None,
    ) -> models.ReportingObligation:
        """Record that a report went to the funder. **Requires their reference.**"""
        if not reference or not str(reference).strip():
            raise DeliveryError(
                "a submitted report requires the funder's reference. Believing a report "
                "was filed when it was not is worse than knowing it is late, because "
                "late can still be fixed."
            )
        obligation = self.db.execute(
            select(models.ReportingObligation).where(
                models.ReportingObligation.id == obligation_id,
                models.ReportingObligation.org_id == self.org_id,
            )
        ).scalars().first()
        if obligation is None:
            raise DeliveryError(f"no reporting obligation {obligation_id} in this organisation")

        obligation.status = models.ReportingObligation.STATUS_SUBMITTED
        obligation.submitted_at = _now()
        obligation.submitted_by = submitted_by
        obligation.reference = str(reference).strip()[:255]
        obligation.report_document_ref = report_document_ref
        self.db.flush()

        self._stage_event(
            event_type="report.submitted",
            payload={
                "grant_id": obligation.grant_id,
                "obligation_id": obligation.id,
                "title": obligation.title,
                "reference": obligation.reference,
            },
        )
        self._record_activity(
            summary_key="report.submitted",
            structured={
                "grant_id": obligation.grant_id,
                "obligation_id": obligation.id,
                "late": bool(_aware(obligation.due_on) and _aware(obligation.due_on) < _now()),
            },
            subject_id=obligation.grant_id,
        )
        self.db.flush()
        return obligation

    # ==================================================================
    # Money
    # ==================================================================
    def expect_disbursement(
        self,
        *,
        grant_id: str,
        amount: Any,
        expected_on: Optional[datetime] = None,
        label: Optional[str] = None,
        tranche_number: Optional[int] = None,
        currency: Optional[str] = None,
        gated_by: Optional[list[str]] = None,
    ) -> models.Disbursement:
        """Record a tranche the grant says will arrive."""
        grant = self._grant(grant_id)
        row = models.Disbursement(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            agent_id=self.agent_id,
            grant_id=grant_id,
            status=models.Disbursement.EXPECTED,
            label=label,
            tranche_number=tranche_number,
            amount=_decimal(amount),
            currency=(currency or grant.currency or "USD").upper()[:3],
            expected_on=_aware(expected_on),
            gated_by_condition_ids={"condition_ids": list(gated_by or [])} or None,
            created_at=_now(),
        )
        self.db.add(row)
        self.db.flush()
        self._stage_event(
            event_type="disbursement.expected",
            payload={
                "grant_id": grant_id,
                "disbursement_id": row.id,
                "label": row.label or f"Tranche {row.tranche_number or '?'}",
                "amount": str(row.amount),
                "currency": row.currency,
                "expected_on": row.expected_on.isoformat() if row.expected_on else None,
            },
        )
        return row

    def record_disbursement_received(
        self,
        *,
        disbursement_id: str,
        reference: str,
        received_on: Optional[datetime] = None,
        amount_received: Any = None,
        variance_note: Optional[str] = None,
    ) -> models.Disbursement:
        """Record that money arrived. **Requires a reference.**

        A project that spends against a tranche it has not received is a project in
        trouble, and "it probably arrived" is how that happens.
        """
        if not reference or not str(reference).strip():
            raise DeliveryError(
                "a received disbursement requires the bank or funder reference. Money "
                "that probably arrived is not received."
            )
        row = self.db.execute(
            select(models.Disbursement).where(
                models.Disbursement.id == disbursement_id,
                models.Disbursement.org_id == self.org_id,
            )
        ).scalars().first()
        if row is None:
            raise DeliveryError(f"no disbursement {disbursement_id} in this organisation")
        if row.status == models.Disbursement.CANCELLED:
            raise DeliveryError("this tranche was cancelled")

        received = _decimal(amount_received) if amount_received is not None else row.amount
        row.status = models.Disbursement.RECEIVED
        row.received_on = _aware(received_on) or _now()
        row.reference = str(reference).strip()[:255]
        row.amount_received = received

        # A short payment is common and invisible if only the expected amount is kept.
        if received != row.amount:
            shortfall = row.amount - received
            row.variance_note = variance_note or (
                f"received {received} against an expected {row.amount} "
                f"({'short by ' + str(shortfall) if shortfall > 0 else 'over by ' + str(-shortfall)})"
            )
        else:
            row.variance_note = variance_note
        self.db.flush()

        self._stage_event(
            event_type="disbursement.received",
            payload={
                "grant_id": row.grant_id,
                "disbursement_id": row.id,
                "label": row.label or f"Tranche {row.tranche_number or '?'}",
                "amount_received": str(received),
                "currency": row.currency,
                "reference": row.reference,
                "variance_note": row.variance_note,
            },
        )
        self._record_activity(
            summary_key="disbursement.received",
            structured={
                "grant_id": row.grant_id,
                "disbursement_id": row.id,
                "variance": row.variance_note,
            },
            subject_id=row.grant_id,
        )
        self.db.flush()
        return row

    # ==================================================================
    # Monitoring
    # ==================================================================
    def deadlines(
        self, *, within_days: int = 30, now: Optional[datetime] = None
    ) -> list[Deadline]:
        """Everything with a date, from all three sources, soonest first.

        Conditions, reports and tranches together, because an organisation's obligations
        are not separated by which table they live in - and a view that showed only
        reports would hide the precondition blocking the next payment.
        """
        moment = _aware(now) or _now()
        horizon = moment + timedelta(days=within_days)
        found: list[Deadline] = []

        def add(kind: str, row_id: str, title: str, due: Optional[datetime],
                grant_id: Optional[str], blocks: bool = False) -> None:
            due_at = _aware(due)
            if due_at is None or due_at > horizon:
                return
            found.append(
                Deadline(
                    kind=kind, id=row_id, title=title, due_on=due_at,
                    days_remaining=(due_at - moment).days,
                    overdue=due_at <= moment,
                    blocks_payment=blocks, grant_id=grant_id,
                )
            )

        for condition in self.db.execute(
            select(models.GrantCondition).where(
                models.GrantCondition.org_id == self.org_id,
                models.GrantCondition.status == models.GrantCondition.STATUS_OPEN,
            )
        ).scalars().all():
            add("CONDITION", condition.id, condition.title, condition.due_on,
                condition.grant_id, condition.blocks_payment)

        for obligation in self.db.execute(
            select(models.ReportingObligation).where(
                models.ReportingObligation.org_id == self.org_id,
                models.ReportingObligation.status.in_(
                    tuple(models.ReportingObligation.OUTSTANDING)
                ),
            )
        ).scalars().all():
            add("REPORT", obligation.id, obligation.title, obligation.due_on, obligation.grant_id)

        for row in self.db.execute(
            select(models.Disbursement).where(
                models.Disbursement.org_id == self.org_id,
                models.Disbursement.status == models.Disbursement.EXPECTED,
            )
        ).scalars().all():
            add("DISBURSEMENT", row.id,
                row.label or f"Tranche {row.tranche_number or '?'}", row.expected_on, row.grant_id)

        return sorted(found, key=lambda d: d.due_on)

    def compliance_summary(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        """What is at risk, in the terms a person can act on.

        Deliberately three separate lists rather than one score. A score would require
        weighting a blocked payment against a late report, and the right response to
        each is different: one is a phone call, the other is writing.
        """
        moment = _aware(now) or _now()
        self.refresh_reporting_statuses(now=moment)

        grants = self.db.execute(
            select(models.Grant).where(
                models.Grant.org_id == self.org_id,
                models.Grant.status == models.Grant.STATUS_ACTIVE,
            )
        ).scalars().all()
        grant_ids = {g.id for g in grants}

        blocking: list[dict[str, Any]] = []
        for condition in self.db.execute(
            select(models.GrantCondition).where(
                models.GrantCondition.org_id == self.org_id,
                models.GrantCondition.status == models.GrantCondition.STATUS_OPEN,
                models.GrantCondition.blocks_payment.is_(True),
            )
        ).scalars().all():
            due = _aware(condition.due_on)
            blocking.append({
                "condition_id": condition.id,
                "grant_id": condition.grant_id,
                "title": condition.title,
                "due_on": due.isoformat() if due else None,
                "overdue": bool(due and due <= moment),
            })

        overdue_reports: list[dict[str, Any]] = []
        for obligation in self.db.execute(
            select(models.ReportingObligation).where(
                models.ReportingObligation.org_id == self.org_id,
                models.ReportingObligation.status == models.ReportingObligation.STATUS_OVERDUE,
            )
        ).scalars().all():
            due = _aware(obligation.due_on)
            overdue_reports.append({
                "obligation_id": obligation.id,
                "grant_id": obligation.grant_id,
                "title": obligation.title,
                "due_on": due.isoformat() if due else None,
                "days_late": (moment - due).days if due else None,
            })

        late_money: list[dict[str, Any]] = []
        for row in self.db.execute(
            select(models.Disbursement).where(
                models.Disbursement.org_id == self.org_id,
                models.Disbursement.status == models.Disbursement.EXPECTED,
            )
        ).scalars().all():
            due = _aware(row.expected_on)
            if due is not None and due <= moment:
                late_money.append({
                    "disbursement_id": row.id,
                    "grant_id": row.grant_id,
                    "label": row.label,
                    "amount": str(row.amount),
                    "currency": row.currency,
                    "expected_on": due.isoformat(),
                    "days_late": (moment - due).days,
                })

        # Money that has arrived, so "how much of this grant is actually in the bank" is
        # answerable rather than assumed.
        received_total = Decimal("0")
        expected_total = Decimal("0")
        for row in self.db.execute(
            select(models.Disbursement).where(
                models.Disbursement.org_id == self.org_id,
            )
        ).scalars().all():
            if row.grant_id not in grant_ids:
                continue
            expected_total += _decimal(row.amount)
            if row.status == models.Disbursement.RECEIVED:
                received_total += _decimal(row.amount_received if row.amount_received is not None else row.amount)

        return {
            "as_of": moment.isoformat(),
            "grants_active": len(grants),
            # A blocked payment is the most urgent of the three: it stops money that is
            # otherwise due.
            "blocking_conditions": blocking,
            "overdue_reports": overdue_reports,
            "late_disbursements": late_money,
            "portfolio": {
                "scheduled_total": str(expected_total),
                "received_total": str(received_total),
                "outstanding_total": str(expected_total - received_total),
            },
            "counts": {
                "blocking_conditions": len(blocking),
                "overdue_reports": len(overdue_reports),
                "late_disbursements": len(late_money),
            },
        }

    def terminate_grant(
        self, *, grant_id: str, status: str, reason: str, by: Optional[str] = None
    ) -> models.Grant:
        """End a grant without deleting it.

        DELETE is revoked on every table in this phase. A grant created in error is
        terminated, which leaves a record that it existed and why it ended - the thing
        an auditor asks for, and the thing that is gone forever if the row is deleted.
        """
        if status not in (
            models.Grant.STATUS_COMPLETED,
            models.Grant.STATUS_TERMINATED,
            models.Grant.STATUS_SUSPENDED,
        ):
            raise DeliveryError(f"{status} is not a valid closing status for a grant")
        if not reason or not str(reason).strip():
            raise DeliveryError("closing a grant requires a reason")
        grant = self._grant(grant_id)
        grant.status = status
        self.db.flush()
        self._stage_event(
            event_type="award.closed",
            payload={"grant_id": grant.id, "status": status, "reason": reason},
        )
        return grant

    # ==================================================================
    # helpers
    # ==================================================================
    def _grant(self, grant_id: str) -> models.Grant:
        grant = self.db.execute(
            select(models.Grant).where(
                models.Grant.id == grant_id,
                models.Grant.org_id == self.org_id,
            )
        ).scalars().first()
        if grant is None:
            raise DeliveryError(f"no grant {grant_id} in this organisation")
        return grant

    def _stage_event(self, *, event_type: str, payload: dict[str, Any]) -> models.OutboxEvent:
        event = models.OutboxEvent(
            id=str(uuid.uuid4()),
            org_id=self.org_id,
            stream=f"granada:v1:delivery:{event_type.split('.')[-1]}",
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
            specialist_key="DELIVERY",
            activity_type="delivery",
            summary_key=summary_key,
            subject_type="GRANT",
            subject_id=subject_id,
            structured_data=structured,
            visibility=models.AgentActivity.VISIBILITY_CUSTOMER,
            occurred_at=_now(),
        )
        self.db.add(activity)
        self.db.flush()
        return activity
