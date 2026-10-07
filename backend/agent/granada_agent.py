"""The persistent Granada Agent.

The product promise, in code
----------------------------
*Create your profile once. Granada creates your agent. Your agent works for you
continuously.*

This module is that agent. One per organisation - enforced by a unique constraint,
not by a convention - owning the organisation's memory, documents, applications,
mail identities, decisions and workflows.

Logical autonomy, shared workers
--------------------------------
Ten thousand NGOs get ten thousand rows in ``granada_agents`` and **one** shared
worker pool. There is deliberately no process, thread, scheduler or Redis lock per
agent anywhere here, and that absence is a design decision rather than an
omission: a process per customer costs a fixed amount whether or not the customer
is doing anything, and it is the model that makes an agent platform expensive to
operate.

What replaces it is one column: ``jobs.agent_id``. A worker picks up a job, loads
the agent named on it, does the work, stores the result, and becomes available for
another organisation. The customer experiences a private 24/7 agent; the operator
runs one fleet.

**A worker's agent must come from the durable record, never from a message.** The
same rule as the tenant, for the same reason: a queued message is transport, and
anything in it is attacker-influenced if the queue is ever reachable. So
:meth:`GranadaAgentService.for_job` reads the agent from the job row.

The specialist roster
---------------------
An agent is not one opaque worker. It has named specialists - Opportunity Hunter,
Matcher, Donor Researcher, Proposal Writer, Budget, Compliance, Document, Email,
Submission, Follow-up - and the customer can see which is doing what. That
visibility is the difference between an agent they trust and a black box they have
to take on faith.

**A specialist can never exceed its parent's authority.** ``autonomy`` lives on
the agent, and :meth:`require_authority` refuses a specialist that asks for more.
An agent cannot be escalated by the component it delegated to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import models
from agent.decision.policy import Autonomy

logger = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """Base class for agent failures."""


class AgentNotFound(AgentError):
    """No agent for that organisation."""


class AgentPaused(AgentError):
    """The agent is not permitted to act."""


class AuthorityExceeded(AgentError):
    """A specialist asked for more authority than its agent holds."""


#: The specialist roster. Ordered as the pipeline runs, so the provider's display
#: order matches the customer's mental model rather than a dictionary's order.
SPECIALISTS: tuple[tuple[str, str], ...] = (
    ("OPPORTUNITY_HUNTER", "Funding Hunter"),
    ("MATCHER", "Matching Agent"),
    ("DONOR_RESEARCHER", "Donor Research Agent"),
    ("PROPOSAL_WRITER", "Proposal Agent"),
    ("BUDGET", "Budget Agent"),
    ("COMPLIANCE", "Compliance Agent"),
    ("DOCUMENT", "Document Agent"),
    ("EMAIL", "Email Agent"),
    ("SUBMISSION", "Submission Agent"),
    ("FOLLOW_UP", "Follow-up Agent"),
)

SPECIALIST_KEYS = tuple(key for key, _ in SPECIALISTS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@dataclass
class AgentStatus:
    """Exactly what the customer sees. Named fields, not prose.

    Every figure is a real count over a real table. ``emails_handled_today`` and
    ``applications_submitted`` are structurally zero until Phases 7 and 8 exist,
    and they are **shown as zero rather than omitted** - a dashboard that hides a
    missing capability is a dashboard that implies it.
    """

    agent_id: str
    display_name: str
    vertical: str
    status: str
    autonomy: str
    #: "Active 24/7" is a claim; this is the evidence for it.
    last_active_at: Optional[datetime]
    #: Distinguishing these two matters: a failed attempt must not read as work
    #: that succeeded.
    last_successful_work: Optional[datetime] = None
    last_attempted_work: Optional[datetime] = None

    opportunities_evaluated_today: int = 0
    hard_rule_rejects: int = 0
    matches: int = 0
    strong_matches: int = 0
    applications_created: int = 0
    research_completed: int = 0
    waiting_for_data: int = 0
    waiting_for_approval: int = 0
    active_workflows: int = 0
    failed_workflows: int = 0

    #: Kept for the original panel's shape, and both stay zero until their
    #: features exist.
    applications_in_progress: int = 0
    emails_handled_today: int = 0
    applications_submitted: int = 0
    actions_requiring_you: int = 0
    opportunities_scanned_today: int = 0
    next_wake_at: Optional[datetime] = None

    @property
    def is_active(self) -> bool:
        return self.status == models.GranadaAgent.ACTIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "vertical": self.vertical,
            "status": self.status,
            "autonomy": self.autonomy,
            "last_active_at": _iso(self.last_active_at),
            "last_successful_work": _iso(self.last_successful_work),
            "last_attempted_work": _iso(self.last_attempted_work),
            "opportunities_evaluated_today": self.opportunities_evaluated_today,
            "hard_rule_rejects": self.hard_rule_rejects,
            "matches": self.matches,
            "strong_matches": self.strong_matches,
            "applications_created": self.applications_created,
            "research_completed": self.research_completed,
            "waiting_for_data": self.waiting_for_data,
            "waiting_for_approval": self.waiting_for_approval,
            "active_workflows": self.active_workflows,
            "failed_workflows": self.failed_workflows,
            "applications_in_progress": self.applications_in_progress,
            "emails_handled_today": self.emails_handled_today,
            "applications_submitted": self.applications_submitted,
            "actions_requiring_you": self.actions_requiring_you,
            "opportunities_scanned_today": self.opportunities_scanned_today,
            "next_wake_at": _iso(self.next_wake_at),
        }


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


class GranadaAgentService:
    """Provisions, reads and drives one organisation's agent."""

    def __init__(self, db: Session, org_id: str) -> None:
        if not org_id:
            raise AgentError("org_id is required; tenant unknown is a deny")
        self.db = db
        self.org_id = org_id

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------
    def provision(
        self,
        *,
        display_name: Optional[str] = None,
        vertical: str = models.GranadaAgent.VERTICAL_NGO,
        autonomy: str = Autonomy.MONITOR_ONLY,
    ) -> models.GranadaAgent:
        """Create the agent and its specialist roster.

        Idempotent, because this is called from registration and registration can
        be retried. Creating a second agent for one organisation is exactly what
        the unique constraint exists to prevent, so this returns the existing one
        rather than racing.
        """
        if vertical not in models.GranadaAgent.VERTICALS:
            raise AgentError(f"unknown vertical {vertical!r}")
        if autonomy not in Autonomy.ORDER:
            raise AgentError(f"unknown autonomy level {autonomy!r}")

        existing = self.get()
        if existing is not None:
            return existing

        organisation = self.db.execute(
            select(models.Organisation).where(models.Organisation.id == self.org_id)
        ).scalars().first()
        if organisation is None:
            raise AgentNotFound(f"no organisation {self.org_id} to provision an agent for")

        # Default the visible name from the organisation, so the customer's first
        # sight of their agent reads like theirs rather than like a UUID.
        name = display_name or f"{organisation.name} Agent"
        agent = models.GranadaAgent(
            org_id=self.org_id,
            display_name=name,
            vertical=vertical,
            status=models.GranadaAgent.ACTIVE,
            autonomy=autonomy,
            settings={},
            version=1,
            created_at=_now(),
            updated_at=_now(),
        )
        self.db.add(agent)
        self.db.flush()

        for key, display in SPECIALISTS:
            self.db.add(
                models.AgentSpecialist(
                    agent_id=agent.id,
                    org_id=self.org_id,
                    key=key,
                    display_name=display,
                    status=models.AgentSpecialist.IDLE,
                    created_at=_now(),
                )
            )
        self.db.flush()
        return agent

    def get(self) -> Optional[models.GranadaAgent]:
        return self.db.execute(
            select(models.GranadaAgent).where(models.GranadaAgent.org_id == self.org_id)
        ).scalars().first()

    def require(self) -> models.GranadaAgent:
        agent = self.get()
        if agent is None:
            raise AgentNotFound(
                f"no Granada Agent for organisation {self.org_id}; provision one before "
                "scheduling work for it"
            )
        return agent

    @classmethod
    def for_job(cls, db: Session, job: models.Job) -> Optional["GranadaAgentService"]:
        """The service for the agent named **on the job row**.

        Never from the job's payload. A worker's tenant already comes from the
        ledger rather than the message; its agent does too, for the same reason: a
        queued payload is transport, and trusting it would let a forged or
        corrupted message act as a different organisation's agent.
        """
        if getattr(job, "agent_id", None):
            agent = db.execute(
                select(models.GranadaAgent).where(models.GranadaAgent.id == job.agent_id)
            ).scalars().first()
            if agent is not None:
                return cls(db, agent.org_id)
        if job.org_id:
            return cls(db, job.org_id)
        return None

    # ------------------------------------------------------------------
    # Authority
    # ------------------------------------------------------------------
    def require_authority(self, specialist_key: str, required: str) -> models.GranadaAgent:
        """Refuse a specialist that wants more authority than its agent holds.

        The parent is the ceiling. An agent must not be escalated by the component
        it delegated to, and this is the check that says so - in code, at the
        point of use, rather than in a document nobody reads at 3am.
        """
        agent = self.require()
        if specialist_key not in SPECIALIST_KEYS:
            raise AgentError(f"unknown specialist {specialist_key!r}")
        if agent.status != models.GranadaAgent.ACTIVE:
            raise AgentPaused(
                f"agent {agent.id} is {agent.status}; it must not act"
            )
        if not Autonomy.at_least(agent.autonomy, required):
            raise AuthorityExceeded(
                f"{specialist_key} requires {required} but the agent holds "
                f"{agent.autonomy}; a specialist cannot exceed its agent's authority"
            )
        return agent

    def set_autonomy(self, level: str) -> models.GranadaAgent:
        """Change the agent's authority. Bumps the version.

        The version bump matters: a cached decision or an in-flight workflow made
        under the old level must be able to tell that the authority changed
        underneath it, rather than completing on authority that has been withdrawn.
        """
        if level not in Autonomy.ORDER:
            raise AgentError(f"unknown autonomy level {level!r}")
        agent = self.require()
        agent.autonomy = level
        agent.version += 1
        agent.updated_at = _now()
        self.db.flush()
        return agent

    # ------------------------------------------------------------------
    # Activity
    # ------------------------------------------------------------------
    def touch(self, *, specialist_key: Optional[str] = None, activity: Optional[str] = None) -> None:
        """Record that the agent worked, so "last worked 3 minutes ago" is true.

        Denormalised onto the agent rather than aggregated from the ledger on
        every page load, because this is read far more often than it is written
        and an aggregate over ``jobs`` would be the slowest thing on the dashboard.
        """
        agent = self.get()
        if agent is None:
            return
        agent.last_active_at = _now()
        agent.updated_at = _now()

        if specialist_key:
            specialist = self.specialist(specialist_key)
            if specialist is not None:
                specialist.status = models.AgentSpecialist.ACTIVE
                specialist.current_activity = activity
                specialist.last_run_at = _now()
                specialist.runs_completed += 1
        self.db.flush()

    def release_specialist(self, specialist_key: str) -> None:
        """Clear the activity string once the work is done.

        Called in a ``finally`` by the worker. An activity string left set is a
        dashboard that lies about what the agent is doing right now.
        """
        specialist = self.specialist(specialist_key)
        if specialist is not None:
            specialist.status = models.AgentSpecialist.IDLE
            specialist.current_activity = None
            self.db.flush()

    def specialist(self, key: str) -> Optional[models.AgentSpecialist]:
        agent = self.get()
        if agent is None:
            return None
        return self.db.execute(
            select(models.AgentSpecialist).where(
                models.AgentSpecialist.agent_id == agent.id,
                models.AgentSpecialist.key == key,
            )
        ).scalars().first()

    def specialists(self) -> list[models.AgentSpecialist]:
        agent = self.get()
        if agent is None:
            return []
        rows = self.db.execute(
            select(models.AgentSpecialist).where(models.AgentSpecialist.agent_id == agent.id)
        ).scalars().all()
        # Ordered by the roster, so the display matches the pipeline.
        order = {key: index for index, (key, _) in enumerate(SPECIALISTS)}
        return sorted(rows, key=lambda row: order.get(row.key, len(order)))

    # ------------------------------------------------------------------
    # Workflows
    # ------------------------------------------------------------------
    def schedule(
        self,
        *,
        workflow_type: str,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        specialist_key: Optional[str] = None,
        run_at: Optional[datetime] = None,
        priority: int = 100,
        context: Optional[dict] = None,
    ) -> models.AgentWorkflow:
        """Create or wake the workflow for this (agent, type, subject).

        Idempotent per subject: two identical workflows would mean the same
        opportunity pursued twice, which is the duplicate-application failure the
        whole platform is judged on.
        """
        agent = self.require()
        if specialist_key is not None and specialist_key not in SPECIALIST_KEYS:
            raise AgentError(f"unknown specialist {specialist_key!r}")

        existing = self.workflow_for(
            workflow_type=workflow_type, subject_type=subject_type, subject_id=subject_id
        )
        if existing is not None:
            # Re-scheduling an existing workflow wakes it rather than duplicating
            # it, and a completed workflow is reopened only deliberately.
            if existing.state == models.AgentWorkflow.COMPLETED:
                return existing
            if run_at is not None:
                existing.next_run_at = _aware(run_at)
            existing.state = models.AgentWorkflow.PENDING
            existing.updated_at = _now()
            if context:
                existing.context = {**(existing.context or {}), **context}
            self.db.flush()
            return existing

        workflow = models.AgentWorkflow(
            agent_id=agent.id,
            org_id=self.org_id,
            specialist_key=specialist_key,
            workflow_type=workflow_type,
            state=models.AgentWorkflow.PENDING,
            subject_type=subject_type,
            subject_id=subject_id,
            next_run_at=_aware(run_at) or _now(),
            priority=priority,
            context=context or {},
            created_at=_now(),
            updated_at=_now(),
        )
        self.db.add(workflow)
        self.db.flush()
        return workflow

    def workflow_for(
        self, *, workflow_type: str, subject_type: Optional[str], subject_id: Optional[str]
    ) -> Optional[models.AgentWorkflow]:
        agent = self.get()
        if agent is None:
            return None
        stmt = select(models.AgentWorkflow).where(
            models.AgentWorkflow.agent_id == agent.id,
            models.AgentWorkflow.workflow_type == workflow_type,
        )
        # NULLs are distinct in a unique constraint, so an unsubjected workflow is
        # matched explicitly rather than by a NULL comparison that never matches.
        stmt = stmt.where(
            models.AgentWorkflow.subject_type.is_(None)
            if subject_type is None
            else models.AgentWorkflow.subject_type == subject_type
        )
        stmt = stmt.where(
            models.AgentWorkflow.subject_id.is_(None)
            if subject_id is None
            else models.AgentWorkflow.subject_id == subject_id
        )
        return self.db.execute(stmt).scalars().first()

    def due_workflows(self, *, at: Optional[datetime] = None, limit: int = 50) -> list[models.AgentWorkflow]:
        """Workflows that should run now for **any** agent.

        Deliberately not scoped to this agent. This is the query the shared
        dispatcher runs: one sweep of the whole fleet, not one sweeper per
        customer. There is no per-agent scheduler anywhere in this design.
        """
        moment = at or _now()
        return list(
            self.db.execute(
                select(models.AgentWorkflow)
                .where(
                    models.AgentWorkflow.state.in_(
                        [models.AgentWorkflow.PENDING, models.AgentWorkflow.WAITING]
                    ),
                    models.AgentWorkflow.next_run_at <= moment,
                )
                .order_by(
                    models.AgentWorkflow.priority.asc(),
                    models.AgentWorkflow.next_run_at.asc(),
                )
                .limit(limit)
            ).scalars()
        )

    def wait(self, workflow: models.AgentWorkflow, *, on: str, until: Optional[datetime] = None) -> models.AgentWorkflow:
        """Park a workflow on a person, a document or a deadline.

        Parking is scheduled rather than blocked: ``next_run_at`` is when Granada
        looks again, so waiting costs a row rather than a process.
        """
        workflow.state = models.AgentWorkflow.WAITING
        workflow.waiting_on = on
        workflow.next_run_at = _aware(until) or (_now() + timedelta(days=1))
        workflow.updated_at = _now()
        self.db.flush()
        return workflow

    # ------------------------------------------------------------------
    # The customer-visible summary
    # ------------------------------------------------------------------
    def status(self, *, today: Optional[datetime] = None) -> AgentStatus:
        """The "Your Granada Agent" panel, computed from durable records.

        Every number here is a real count over a real table. Nothing is
        estimated, and nothing is a placeholder: a status panel that flatters the
        agent is worse than no panel, because the customer makes decisions on it.

        ``emails_handled_today`` and ``applications_submitted`` are counted from
        the tables that would hold them, so they read **zero** until Phases 7 and 8
        populate those tables - rather than being absent, which would let the UI
        imply the capability exists.
        """
        agent = self.require()
        moment = today or _now()
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)

        def count(model, *conditions) -> int:
            return int(
                self.db.execute(
                    select(func.count(model.id)).where(*conditions)
                ).scalar() or 0
            )

        # -- matching, from the machine-written resource summaries ------------
        # Read from agent_activity rather than re-deriving from opportunity_matches,
        # because the activity ledger is the *record of what the agent did* and the
        # panel should report the agent's work, not a parallel computation that
        # could disagree with it.
        evaluated = count(
            models.AgentActivity,
            models.AgentActivity.org_id == self.org_id,
            models.AgentActivity.occurred_at >= midnight,
            models.AgentActivity.summary_key.in_(
                ["match.passed", "match.rejected_by_rule", "match.needs_data"]
            ),
        )
        rejected = count(
            models.AgentActivity,
            models.AgentActivity.org_id == self.org_id,
            models.AgentActivity.occurred_at >= midnight,
            models.AgentActivity.summary_key == "match.rejected_by_rule",
        )
        research_done = count(
            models.AgentActivity,
            models.AgentActivity.org_id == self.org_id,
            models.AgentActivity.summary_key == "research.completed",
        )

        matches = count(
            models.OpportunityMatch,
            models.OpportunityMatch.org_id == self.org_id,
            models.OpportunityMatch.state == models.OpportunityMatch.MATCHED,
        )
        # "Strong" is deliberately a high semantic score rather than merely a
        # passed gate: everything in that table already passed the gates, so a
        # count of them would be the same number twice.
        strong = count(
            models.OpportunityMatch,
            models.OpportunityMatch.org_id == self.org_id,
            models.OpportunityMatch.state == models.OpportunityMatch.MATCHED,
            models.OpportunityMatch.semantic_score >= 0.85,
        )
        waiting_data = count(
            models.OpportunityMatch,
            models.OpportunityMatch.org_id == self.org_id,
            models.OpportunityMatch.state == models.OpportunityMatch.NEEDS_DATA,
        )

        applications = count(
            models.Application, models.Application.org_id == self.org_id
        )
        in_progress = count(
            models.Application,
            models.Application.org_id == self.org_id,
            models.Application.closed_at.is_(None),
        )
        submitted = count(
            models.Application,
            models.Application.org_id == self.org_id,
            models.Application.submitted_at.isnot(None),
        )
        pending_approval = count(
            models.Application,
            models.Application.org_id == self.org_id,
            models.Application.state.in_(
                ["WAITING_FOR_APPROVAL", "RESPONSE_WAITING_APPROVAL"]
            ),
        )

        # Mail lands in Phase 7; counted from the table that will hold it, so it
        # is honestly zero rather than invented.
        emails = count(
            models.DecisionRecord,
            models.DecisionRecord.organisation_id == self.org_id,
            models.DecisionRecord.decision_type == "email_triage",
            models.DecisionRecord.created_at >= midnight,
        )
        escalated = count(
            models.DecisionRecord,
            models.DecisionRecord.organisation_id == self.org_id,
            models.DecisionRecord.shadow.is_(False),
            models.DecisionRecord.policy_outcome.is_(False),
        )
        # Mail that Granada received and could NOT confidently attach to an
        # application is an action item, and leaving it out of this figure was a
        # real gap: the panel showed zero things needing attention while an
        # unlinked funder email sat unread. "Wrong linkage is worse than no
        # linkage" only holds if a person is actually told to make the link.
        ambiguous_mail = count(
            models.MailApplicationLink,
            models.MailApplicationLink.org_id == self.org_id,
            models.MailApplicationLink.confidence.in_(["AMBIGUOUS", "UNLINKED"]),
            models.MailApplicationLink.status == models.MailApplicationLink.STATUS_ACTIVE,
        )

        active_workflows = count(
            models.AgentWorkflow,
            models.AgentWorkflow.agent_id == agent.id,
            models.AgentWorkflow.state.in_(
                [models.AgentWorkflow.PENDING, models.AgentWorkflow.RUNNING,
                 models.AgentWorkflow.WAITING, models.AgentWorkflow.BLOCKED]
            ),
        )
        failed_workflows = count(
            models.AgentWorkflow,
            models.AgentWorkflow.agent_id == agent.id,
            models.AgentWorkflow.state == models.AgentWorkflow.FAILED,
        )

        next_wake = self.db.execute(
            select(func.min(models.AgentWorkflow.next_run_at)).where(
                models.AgentWorkflow.agent_id == agent.id,
                models.AgentWorkflow.next_run_at.isnot(None),
                models.AgentWorkflow.state.in_(
                    [models.AgentWorkflow.PENDING, models.AgentWorkflow.WAITING]
                ),
            )
        ).scalar()

        # Successful versus attempted work, from the ledger's own attempt records
        # rather than from the job's current state - a job that failed and was then
        # requeued is both.
        last_success = self.db.execute(
            select(func.max(models.JobAttempt.finished_at))
            .select_from(models.JobAttempt)
            .join(models.Job, models.Job.id == models.JobAttempt.job_id)
            .where(models.Job.agent_id == agent.id, models.JobAttempt.outcome == "SUCCEEDED")
        ).scalar()
        last_attempt = self.db.execute(
            select(func.max(models.JobAttempt.started_at))
            .select_from(models.JobAttempt)
            .join(models.Job, models.Job.id == models.JobAttempt.job_id)
            .where(models.Job.agent_id == agent.id)
        ).scalar()

        return AgentStatus(
            agent_id=agent.id,
            display_name=agent.display_name,
            vertical=agent.vertical,
            status=agent.status,
            autonomy=agent.autonomy,
            last_active_at=_aware(agent.last_active_at),
            last_successful_work=_aware(last_success),
            last_attempted_work=_aware(last_attempt),
            opportunities_evaluated_today=evaluated,
            hard_rule_rejects=rejected,
            matches=matches,
            strong_matches=strong,
            applications_created=applications,
            research_completed=research_done,
            waiting_for_data=waiting_data,
            waiting_for_approval=pending_approval,
            active_workflows=active_workflows,
            failed_workflows=failed_workflows,
            applications_in_progress=in_progress,
            emails_handled_today=emails,
            applications_submitted=submitted,
            actions_requiring_you=int(
                pending_approval + waiting_data + escalated + ambiguous_mail
            ),
            opportunities_scanned_today=evaluated,
            next_wake_at=_aware(next_wake) if next_wake else None,
        )
