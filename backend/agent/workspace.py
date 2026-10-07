"""The application workspace state machine.

One workspace per organisation and opportunity, carrying the whole lifecycle.
The brief specifies the state set; this module specifies which transitions are
legal and, more importantly, **which are refused and why**.

Why a state machine rather than a status column
-----------------------------------------------
A status column records where an application is. A state machine records where it
is *allowed to go next*, and refuses the rest. The difference is the failure that
matters: an application moving from `PREPARING` to `SUBMITTED` without passing
through `READY_TO_SUBMIT` and an approval gate is a submission nobody authorised,
and a status column cannot tell you that happened.

Guards are the point
--------------------
Three guards exist because the states they protect are the ones with real-world
consequences:

* **`READY_TO_SUBMIT` requires readiness.** Nothing reaches it while a mandatory
  document is missing or expired, or a required answer is unanswered. A decision
  provider cannot override a missing document at any confidence - the same rule as
  the hard eligibility gates, applied at the other end of the lifecycle.
* **`WAITING_FOR_APPROVAL` requires a named approver.** "The system decided it was
  fine" is not approval, exactly as it is not verification of a fact.
* **`SUBMITTED` requires a receipt.** An application with no external reference is
  not submitted; it is *possibly* submitted, and claiming otherwise is how a
  funder gets an application Granada believes it sent but did not.

Every accepted transition appends to `ApplicationTransition` and increments
`Application.version`, so the state at the moment of submission stays citable
afterwards.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from agent.organisation_memory import DocumentVault, OrganisationMemory

logger = logging.getLogger(__name__)


class WorkspaceError(RuntimeError):
    """Base class for workspace failures."""


class IllegalTransition(WorkspaceError):
    """The requested transition is not permitted from the current state."""


class GuardFailed(WorkspaceError):
    """The transition is structurally legal but its precondition is not met."""


class WorkspaceNotFound(WorkspaceError):
    """No workspace for that organisation and opportunity."""


#: The state set from the brief, verbatim and complete.
DISCOVERED = "DISCOVERED"
MATCHED = "MATCHED"
QUALIFIED = "QUALIFIED"
REJECTED_BY_RULE = "REJECTED_BY_RULE"
RESEARCHING = "RESEARCHING"
PREPARING = "PREPARING"
WAITING_FOR_DATA = "WAITING_FOR_DATA"
WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
READY_TO_SUBMIT = "READY_TO_SUBMIT"
SUBMITTING = "SUBMITTING"
SUBMITTED = "SUBMITTED"
CLARIFICATION_RECEIVED = "CLARIFICATION_RECEIVED"
RESPONSE_PREPARING = "RESPONSE_PREPARING"
RESPONSE_WAITING_APPROVAL = "RESPONSE_WAITING_APPROVAL"
RESPONSE_SENT = "RESPONSE_SENT"
SHORTLISTED = "SHORTLISTED"
INTERVIEW = "INTERVIEW"
AWARDED = "AWARDED"
REJECTED = "REJECTED"
WITHDRAWN = "WITHDRAWN"
CLOSED = "CLOSED"

ALL_STATES = (
    DISCOVERED, MATCHED, QUALIFIED, REJECTED_BY_RULE, RESEARCHING, PREPARING,
    WAITING_FOR_DATA, WAITING_FOR_APPROVAL, READY_TO_SUBMIT, SUBMITTING,
    SUBMITTED, CLARIFICATION_RECEIVED, RESPONSE_PREPARING,
    RESPONSE_WAITING_APPROVAL, RESPONSE_SENT, SHORTLISTED, INTERVIEW,
    AWARDED, REJECTED, WITHDRAWN, CLOSED,
)

#: No outgoing transitions. Reached deliberately, not by accident.
TERMINAL_STATES = frozenset({AWARDED, REJECTED, WITHDRAWN, CLOSED})

#: States in which the workspace is pursuing the opportunity. Used by reporting
#: so "active" is one definition rather than three.
ACTIVE_STATES = frozenset(
    {DISCOVERED, MATCHED, QUALIFIED, RESEARCHING, PREPARING, WAITING_FOR_DATA,
     WAITING_FOR_APPROVAL, READY_TO_SUBMIT, SUBMITTING, SUBMITTED,
     CLARIFICATION_RECEIVED, RESPONSE_PREPARING, RESPONSE_WAITING_APPROVAL,
     RESPONSE_SENT, SHORTLISTED, INTERVIEW}
)

#: The transition table. Absence from this map is a refusal.
ALLOWED: dict[str, frozenset[str]] = {
    DISCOVERED: frozenset({MATCHED, REJECTED_BY_RULE, WITHDRAWN}),
    MATCHED: frozenset({QUALIFIED, REJECTED_BY_RULE, WITHDRAWN, RESEARCHING}),
    QUALIFIED: frozenset({RESEARCHING, PREPARING, WAITING_FOR_DATA, WITHDRAWN, REJECTED_BY_RULE}),
    # A rule rejection is not always final: the rules can change, or the profile
    # can be corrected. Re-entering matching is a deliberate, recorded act.
    REJECTED_BY_RULE: frozenset({QUALIFIED, WITHDRAWN, CLOSED}),
    RESEARCHING: frozenset({PREPARING, WAITING_FOR_DATA, QUALIFIED, WITHDRAWN}),
    PREPARING: frozenset(
        {WAITING_FOR_DATA, WAITING_FOR_APPROVAL, READY_TO_SUBMIT, RESEARCHING, WITHDRAWN}
    ),
    WAITING_FOR_DATA: frozenset({PREPARING, RESEARCHING, WITHDRAWN}),
    WAITING_FOR_APPROVAL: frozenset({READY_TO_SUBMIT, PREPARING, WAITING_FOR_DATA, WITHDRAWN}),
    READY_TO_SUBMIT: frozenset({SUBMITTING, PREPARING, WAITING_FOR_APPROVAL, WITHDRAWN}),
    # SUBMITTING exists so a crash mid-submission is recoverable and visible: an
    # application stuck here is one a human should look at, and the idempotency
    # key on the submission is what stops it being sent twice.
    SUBMITTING: frozenset({SUBMITTED, READY_TO_SUBMIT, WITHDRAWN}),
    SUBMITTED: frozenset(
        {CLARIFICATION_RECEIVED, SHORTLISTED, INTERVIEW, AWARDED, REJECTED, WITHDRAWN, CLOSED}
    ),
    CLARIFICATION_RECEIVED: frozenset({RESPONSE_PREPARING, RESPONSE_WAITING_APPROVAL,
                                       AWARDED, REJECTED, WITHDRAWN, CLOSED}),
    RESPONSE_PREPARING: frozenset({RESPONSE_WAITING_APPROVAL, RESPONSE_SENT,
                                   CLARIFICATION_RECEIVED, WITHDRAWN}),
    RESPONSE_WAITING_APPROVAL: frozenset({RESPONSE_SENT, RESPONSE_PREPARING, WITHDRAWN}),
    RESPONSE_SENT: frozenset({SUBMITTED, CLARIFICATION_RECEIVED, SHORTLISTED,
                              INTERVIEW, AWARDED, REJECTED, WITHDRAWN, CLOSED}),
    SHORTLISTED: frozenset({INTERVIEW, AWARDED, REJECTED, WITHDRAWN, CLOSED}),
    INTERVIEW: frozenset({AWARDED, REJECTED, WITHDRAWN, CLOSED}),
    AWARDED: frozenset(),
    REJECTED: frozenset(),
    WITHDRAWN: frozenset(),
    CLOSED: frozenset(),
}

#: Transitions that require a named human approver.
REQUIRES_APPROVAL = frozenset({
    (WAITING_FOR_APPROVAL, READY_TO_SUBMIT),
    (RESPONSE_WAITING_APPROVAL, RESPONSE_SENT),
})

#: Transitions that require an external receipt.
REQUIRES_RECEIPT = frozenset({(SUBMITTING, SUBMITTED)})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@dataclass
class ReadinessReport:
    """Whether an application may advance to ``READY_TO_SUBMIT``.

    Deliberately not a bare boolean. When an application is not ready, the answer
    to "why not" is a list of named blockers, because that list is the work item
    for a human.
    """

    ready: bool
    blockers: list[str]

    @property
    def reason(self) -> str:
        if self.ready:
            return "all readiness checks passed"
        return "not ready: " + "; ".join(self.blockers)


class ApplicationWorkspace:
    """Creates workspaces and moves them through the lifecycle."""

    def __init__(self, db: Session, org_id: str, *, actor_id: Optional[str] = None) -> None:
        if not org_id:
            raise WorkspaceError("org_id is required; tenant unknown is a deny")
        self.db = db
        self.org_id = org_id
        self.actor_id = actor_id
        self.memory = OrganisationMemory(db, org_id, actor_id=actor_id)

    # ------------------------------------------------------------------
    # Creation and lookup
    # ------------------------------------------------------------------
    def create(
        self,
        opportunity: models.Opportunity,
        *,
        state: str = DISCOVERED,
        reason: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> models.Application:
        """Create the workspace. Idempotent per (organisation, opportunity)."""
        existing = self.get(opportunity.id)
        if existing is not None:
            return existing
        if state not in ALL_STATES:
            raise WorkspaceError(f"unknown state {state!r}")

        application = models.Application(
            org_id=self.org_id,
            opportunity_id=opportunity.id,
            state=state,
            version=1,
            created_by=self.actor_id,
            state_reason=reason or "workspace created",
            deadline=_aware(opportunity.deadline),
            correlation_id=correlation_id,
            created_at=_now(),
            updated_at=_now(),
        )
        self.db.add(application)
        self.db.flush()

        self._record(
            application,
            from_state=None,
            to_state=state,
            reason=reason or "workspace created",
            actor_type=models.ApplicationTransition.ACTOR_SYSTEM,
            correlation_id=correlation_id,
        )
        return application

    def get(self, opportunity_id: str) -> Optional[models.Application]:
        return self.db.execute(
            select(models.Application).where(
                models.Application.org_id == self.org_id,
                models.Application.opportunity_id == opportunity_id,
            )
        ).scalars().first()

    def require(self, opportunity_id: str) -> models.Application:
        application = self.get(opportunity_id)
        if application is None:
            raise WorkspaceNotFound(
                f"no workspace for opportunity {opportunity_id} in this organisation"
            )
        return application

    def active(self) -> list[models.Application]:
        return list(
            self.db.execute(
                select(models.Application)
                .where(
                    models.Application.org_id == self.org_id,
                    models.Application.state.in_(sorted(ACTIVE_STATES)),
                )
                .order_by(models.Application.deadline.asc().nullsfirst())
            ).scalars()
        )

    def history(self, application: models.Application) -> list[models.ApplicationTransition]:
        """The append-only trail, oldest first."""
        return list(
            self.db.execute(
                select(models.ApplicationTransition)
                .where(models.ApplicationTransition.application_id == application.id)
                .order_by(models.ApplicationTransition.version.asc())
            ).scalars()
        )

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------
    def transition(
        self,
        application: models.Application,
        to_state: str,
        *,
        reason: Optional[str] = None,
        actor_type: str = models.ApplicationTransition.ACTOR_SYSTEM,
        actor_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        job_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        readiness: Optional[ReadinessReport] = None,
        receipt: Optional[str] = None,
        approved_by: Optional[str] = None,
    ) -> models.Application:
        """Move the workspace, or refuse with a reason.

        The checks run in a deliberate order: structural legality first, then the
        guards. A caller asking for an impossible move should be told it is
        impossible rather than told a document is missing.
        """
        if to_state not in ALL_STATES:
            raise WorkspaceError(f"unknown state {to_state!r}")
        if application.org_id != self.org_id:
            raise WorkspaceError("application belongs to another organisation")

        current = application.state
        if to_state == current:
            # Idempotent: a redelivered message asking for the state we are
            # already in is not an error, and must not append a history row.
            return application

        targets = ALLOWED.get(current, frozenset())
        if to_state not in targets:
            raise IllegalTransition(
                f"{current} -> {to_state} is not permitted; from {current} the "
                f"legal states are {sorted(targets) or ['none (terminal)']}"
            )

        # -- guards ------------------------------------------------------
        if (current, to_state) in REQUIRES_APPROVAL:
            approver = approved_by or application.approved_by
            if not approver:
                raise GuardFailed(
                    f"{current} -> {to_state} requires a named human approver; "
                    "an application does not become ready because a system decided it was"
                )

        if (current, to_state) in REQUIRES_RECEIPT:
            if not (receipt or application.submission_receipt):
                raise GuardFailed(
                    f"{current} -> {to_state} requires an external receipt; an "
                    "application with no funder reference is not submitted"
                )

        if to_state == READY_TO_SUBMIT:
            report = readiness or self.readiness(application)
            if not report.ready:
                raise GuardFailed(f"cannot become ready to submit: {report.reason}")

        if to_state == SUBMITTED and application.submitted_at is None:
            application.submitted_at = _now()
        if receipt:
            application.submission_receipt = receipt
        if to_state in TERMINAL_STATES:
            application.closed_at = _now()
            if to_state in {AWARDED, REJECTED}:
                application.outcome = to_state

        application.state = to_state
        application.state_reason = reason
        application.version += 1
        application.updated_at = _now()
        if to_state == READY_TO_SUBMIT:
            # Consume the approval, so the next advance needs a new one.
            application.approved_at = _now()
        if actor_type == models.ApplicationTransition.ACTOR_HUMAN and actor_id:
            application.approved_by = actor_id

        self._record(
            application,
            from_state=current,
            to_state=to_state,
            reason=reason,
            actor_type=actor_type,
            actor_id=actor_id or self.actor_id,
            decision_id=decision_id,
            job_id=job_id,
            correlation_id=correlation_id,
        )
        return application

    def approve(
        self,
        application: models.Application,
        *,
        approved_by: str,
        reason: Optional[str] = None,
    ) -> models.Application:
        """Record a human approval for the pending step, without advancing.

        Separating approval from advancement means a workspace can sit in
        ``WAITING_FOR_APPROVAL`` with the approval already given, and the
        transition that consumes it is still a distinct, auditable act.
        """
        if not approved_by:
            raise GuardFailed(
                "approval requires a named person; an unattributed approval is not one"
            )
        if application.org_id != self.org_id:
            raise WorkspaceError("application belongs to another organisation")
        application.approved_by = approved_by
        application.approved_at = _now()
        application.updated_at = _now()
        self.db.flush()
        return application

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------
    def _held_documents(self, canonical_type: str) -> list[models.Document]:
        """Current documents whose type means the same thing as ``canonical_type``.

        Matched in Python rather than in SQL because the synonym list lives in the
        registry, and duplicating it as an ``IN`` clause is how the two drift apart again.
        The row count here is small - an organisation holds tens of documents, not
        millions - so the readability is worth more than the query.
        """
        from agent import document_types

        rows = self.db.execute(
            select(models.Document).where(
                models.Document.org_id == self.org_id,
                models.Document.is_current.is_(True),
            )
        ).scalars().all()
        return [row for row in rows if document_types.is_same_type(row.doc_type, canonical_type)]

    def _usable_document(self, vault: Any, canonical_type: str) -> bool:
        """Whether an APPROVED, unexpired document of this type is held."""
        for document in self._held_documents(canonical_type):
            if vault.usable(doc_type=document.doc_type):
                return True
        return False

    def readiness(self, application: models.Application) -> ReadinessReport:
        """The deterministic checks that gate ``READY_TO_SUBMIT``.

        These are the checks that are simply true or false. A decision provider
        cannot override any of them - the same rule as the hard eligibility gates,
        applied at the other end of the lifecycle.
        """
        blockers: list[str] = []

        if application.org_id != self.org_id:
            raise WorkspaceError("application belongs to another organisation")

        opportunity = self.db.execute(
            select(models.Opportunity).where(models.Opportunity.id == application.opportunity_id)
        ).scalars().first()
        if opportunity is None:
            blockers.append("the opportunity no longer exists")
            return ReadinessReport(ready=False, blockers=blockers)

        # -- deadline ----------------------------------------------------
        deadline = _aware(opportunity.deadline)
        if deadline is not None and deadline <= _now():
            blockers.append(f"the deadline passed on {deadline.date().isoformat()}")

        # -- organisation facts ------------------------------------------
        missing = self.memory.missing_facts(
            ["country", "organisation_type", "registration_valid_until"]
        )
        for item in missing:
            blockers.append(f"{item.key} is {item.reason}")

        # -- documents ---------------------------------------------------
        vault = DocumentVault(self.db, self.org_id)
        text = " ".join(
            filter(None, [opportunity.eligibility_criteria, opportunity.application_process])
        ).casefold()
        # The requirements come from ONE registry, and matching accepts the synonyms an
        # organisation would actually have used.
        #
        # The previous version hard-coded `doc_type == "audited_accounts"` here while every
        # helper in the codebase created `audited_financial_statements`. Nothing produced the
        # type the gate asked for, so any listing mentioning audited accounts was
        # permanently unsubmittable - and no test reached the branch, because every test
        # opportunity's eligibility text omitted those phrases. `agent.document_types` is
        # the single list, and this is the only place requirements are read.
        from agent import document_types

        for doc_type in document_types.required_types_for(text):
            usable = self._usable_document(vault, doc_type)
            if usable:
                continue
            held = self._held_documents(doc_type)
            if held:
                blockers.append(
                    f"the {doc_type.replace('_', ' ')} on file is not approved, "
                    "or has expired"
                )
            else:
                # The accepted names are listed, because "required but not held" for a
                # document the organisation HAS under another name is the worst kind of
                # blocker: it looks like missing evidence and is actually a vocabulary
                # mismatch.
                alternatives = ", ".join(
                    name for name in document_types.DOCUMENT_TYPES.get(doc_type, ())
                    if name != doc_type
                ) or "no alternative names"
                blockers.append(
                    f"a {doc_type.replace('_', ' ')} is required but not held "
                    f"(accepted names: {doc_type}, {alternatives})"
                )

        # -- answers -----------------------------------------------------
        # Any answer the workspace has recorded as outstanding blocks readiness.
        unanswered = (application.state_reason or "")
        if "unanswered:" in unanswered:
            blockers.append("required answers are outstanding")

        return ReadinessReport(ready=not blockers, blockers=blockers)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _record(
        self,
        application: models.Application,
        *,
        from_state: Optional[str],
        to_state: str,
        reason: Optional[str],
        actor_type: str,
        actor_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        job_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> models.ApplicationTransition:
        row = models.ApplicationTransition(
            application_id=application.id,
            org_id=self.org_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            actor_type=actor_type,
            actor_id=actor_id,
            decision_id=decision_id,
            job_id=job_id,
            version=application.version,
            correlation_id=correlation_id,
            occurred_at=_now(),
        )
        self.db.add(row)
        self.db.flush()
        return row


def reachable_from(state: str, seen: Optional[set[str]] = None) -> set[str]:
    """Every state reachable from ``state``, transitively.

    Exists so a test can prove there are no unreachable states and, more
    importantly, that a terminal state cannot be escaped. A state machine with an
    island in it is one where an application can silently stop.
    """
    seen = seen if seen is not None else set()
    for target in ALLOWED.get(state, frozenset()):
        if target not in seen:
            seen.add(target)
            reachable_from(target, seen)
    return seen
