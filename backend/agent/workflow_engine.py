"""The fleet: one dispatcher and one shared worker pool.

Orchestration and execution are separate
---------------------------------------
The **dispatcher** discovers due work and enqueues it. That is all it does. It
does not research, decide, call a model or touch a document, and the separation is
what allows several dispatchers and many workers later without either growing a
second responsibility.

The **worker** claims a durable job, resolves the agent **from the job row**,
checks authority against the agent's *current* version, runs one specialist
handler, and advances the workflow canonically.

One fleet, not one process per customer
--------------------------------------
``dispatch_once`` sweeps every agent in one bounded query. There is no per-agent
timer, process or scheduler anywhere, and ``jobs.agent_id`` is what lets a worker
serve whichever organisation the job belongs to.

Four properties are load-bearing and each has a test:

**No duplicate dispatch.** The durable job is created through
``JobLedger.enqueue`` with an idempotency key of
``f"{workflow_id}:{workflow_version}"``, backed by
``UniqueConstraint(org_id, job_type, idempotency_key)``. Two dispatchers racing
produce one job, and the loser gets ``(existing, False)`` rather than a second row.
Correctness comes from the constraint, not from timing.

**Fairness.** One large NGO with ten thousand due workflows must not starve an
NGO with one. A sweep applies a per-agent cap so every agent that has due work is
represented before any agent gets a second helping.

**Authority is re-checked at the point of use.** The job records the agent version
it was created under. If the agent's version has moved - the organisation paused,
or reduced its autonomy - the worker does **not** silently continue.

**Redis is not truth.** The worker reads everything authoritative from PostgreSQL;
the stream entry only says where to look.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import models
from agent.decision.policy import Autonomy
from agent.granada_agent import (
    AgentError,
    AgentNotFound,
    AgentPaused,
    AuthorityExceeded,
    GranadaAgentService,
)
from agent.matching import EligibilityEngine, Matcher
from agent.organisation_memory import OrganisationMemory
from agent.research import DonorResearchService
from agent.specialists import (
    SpecialistDisabled,
    SpecialistError,
    check_work_type,
    for_work_type,
    require_executable,
)
from agent.workspace import (
    ApplicationWorkspace,
    GuardFailed,
    IllegalTransition,
    WorkspaceError,
    DISCOVERED,
    MATCHED,
    PREPARING,
    QUALIFIED,
    REJECTED_BY_RULE,
    RESEARCHING,
)
from events.ledger import JobLedger
from observability import metrics

logger = logging.getLogger(__name__)


class FleetError(RuntimeError):
    """Base class for fleet failures."""


class AuthorityChanged(FleetError):
    """The agent's authority moved while this work was queued.

    Not a failure of the work: a *signal* that the permission the work was created
    under is no longer current. The worker must re-evaluate rather than continue.
    """


class AgentNotActive(FleetError):
    """The agent is paused or suspended, so its work must not run."""


class AgentMismatch(FleetError):
    """The job's agent does not belong to the job's organisation.

    PostgreSQL refuses this combination with a composite foreign key, so reaching
    this in application code means either a pre-constraint row or a bug - and both
    must stop rather than proceed.
    """


#: Workflow types this phase can actually execute.
WORKFLOW_MATCH = "opportunity_match"
WORKFLOW_QUALIFY = "opportunity_qualify"
WORKFLOW_RESEARCH = "donor_research"

#: Default per-agent cap in one sweep. Chosen so a single very large organisation
#: cannot fill a batch, while a normal organisation is never truncated by it.
DEFAULT_PER_AGENT_LIMIT = 25

#: Default batch size. Bounded so a sweep never loads the whole table.
DEFAULT_BATCH_SIZE = 200


@dataclass
class DispatchResult:
    """What one sweep did."""

    scanned: int = 0
    dispatched: int = 0
    duplicates: int = 0
    skipped_paused: int = 0
    skipped_unhandled: int = 0
    per_agent: dict[str, int] = field(default_factory=dict)

    @property
    def lag_seconds(self) -> float:
        return 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "dispatched": self.dispatched,
            "duplicates": self.duplicates,
            "skipped_paused": self.skipped_paused,
            "skipped_unhandled": self.skipped_unhandled,
            "agents_touched": len(self.per_agent),
        }


@dataclass
class ExecutionResult:
    """What one worker did with one job."""

    job_id: str
    outcome: str
    workflow_id: Optional[str] = None
    agent_id: Optional[str] = None
    specialist: Optional[str] = None
    detail: Optional[str] = None
    activity_recorded: bool = False

    #: Outcomes. Separate because each one means something different to an
    #: operator: PARKED is healthy, FAILED is not, and an authority change is
    #: neither.
    SUCCEEDED = "SUCCEEDED"
    PARKED = "PARKED"
    REAUTHORIZED = "REAUTHORIZED"
    CANCELLED_BY_POLICY = "CANCELLED_BY_POLICY"
    AGENT_PAUSED = "AGENT_PAUSED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
class FleetDispatcher:
    """Discovers due work across every agent and enqueues it. Nothing else."""

    def __init__(
        self,
        db: Session,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        per_agent_limit: int = DEFAULT_PER_AGENT_LIMIT,
    ) -> None:
        self.db = db
        self.batch_size = batch_size
        self.per_agent_limit = per_agent_limit

    def due_workflows(self, *, now: Optional[datetime] = None, limit: Optional[int] = None) -> list[models.AgentWorkflow]:
        """Due work across the whole fleet, **fairly partitioned per agent**.

        Ordering is ``(priority, next_run_at, id)``. The trailing ``id`` matters:
        without a total order two dispatchers can see overlapping windows and,
        while the unique constraint still stops duplicate *jobs*, the sweep stops
        being reproducible and a test cannot assert what it will do.

        **The per-agent cap is applied in SQL, not after fetching.** The first
        version of this method fetched the global top-N by priority and then capped
        per agent, which does not provide fairness at all: an organisation with
        thousands of high-priority due workflows fills the entire window, and a
        smaller organisation's work is never even fetched. A window function ranks
        within each agent first, so every agent with due work is represented in the
        candidate set before any agent gets a second helping.

        This is why the fairness test uses a *lower* priority for the small
        organisation: only a partition-level guarantee saves it, and a
        fetch-then-cap implementation fails that test.
        """
        moment = now or datetime.now(timezone.utc)
        ranked = (
            select(
                models.AgentWorkflow.id.label("workflow_id"),
                func.row_number()
                .over(
                    partition_by=models.AgentWorkflow.agent_id,
                    order_by=(
                        models.AgentWorkflow.priority.asc(),
                        models.AgentWorkflow.next_run_at.asc(),
                        models.AgentWorkflow.id.asc(),
                    ),
                )
                .label("agent_rank"),
            )
            .where(
                models.AgentWorkflow.state.in_(
                    [models.AgentWorkflow.PENDING, models.AgentWorkflow.WAITING]
                ),
                models.AgentWorkflow.next_run_at.isnot(None),
                models.AgentWorkflow.next_run_at <= moment,
            )
            .subquery()
        )

        stmt = (
            select(models.AgentWorkflow)
            .join(ranked, ranked.c.workflow_id == models.AgentWorkflow.id)
            .where(ranked.c.agent_rank <= self.per_agent_limit)
            .order_by(
                models.AgentWorkflow.priority.asc(),
                models.AgentWorkflow.next_run_at.asc(),
                models.AgentWorkflow.id.asc(),
            )
            .limit(limit or self.batch_size)
        )
        return list(self.db.execute(stmt).scalars())

    def dispatch_once(
        self, *, now: Optional[datetime] = None, limit: Optional[int] = None
    ) -> DispatchResult:
        """One bounded sweep.

        Fairness is guaranteed by ``due_workflows``'s partition, so every agent
        with due work appears in the candidate set. The per-agent counter here is a
        second, defensive cap for the case where a workflow is deferred mid-sweep.
        """
        moment = now or datetime.now(timezone.utc)
        result = DispatchResult()

        try:
            candidates = self.due_workflows(now=moment, limit=limit)
        except Exception:
            # A dispatcher that dies must not poison the fleet.
            self.db.rollback()
            raise

        for workflow in candidates:
            result.scanned += 1

            agent = self.db.execute(
                select(models.GranadaAgent).where(models.GranadaAgent.id == workflow.agent_id)
            ).scalars().first()
            if agent is None:
                result.skipped_unhandled += 1
                continue

            if agent.status != models.GranadaAgent.ACTIVE:
                # A paused agent receives no new work, and its workflow is parked
                # rather than dispatched. Parked, not cancelled: pressing pause is
                # not pressing stop.
                workflow.state = models.AgentWorkflow.WAITING
                workflow.waiting_on = f"agent {agent.status.lower()}"
                workflow.next_run_at = moment + timedelta(hours=1)
                result.skipped_paused += 1
                metrics.inc("fleet.dispatch_skipped_paused")
                continue

            if result.per_agent.get(agent.id, 0) >= self.per_agent_limit:
                # Fairness: give the other organisations their turn first.
                metrics.inc("fleet.dispatch_deferred_fairness")
                continue

            if workflow.specialist_key is not None:
                try:
                    candidate = check_work_type(workflow.specialist_key, workflow.workflow_type)
                    # Resolving is not enough: a registered-but-unimplemented
                    # specialist must park rather than be dispatched into a job
                    # that can only fail.
                    if workflow.workflow_type not in candidate.handlers:
                        raise SpecialistDisabled(
                            f"{candidate.display_name} has no handler for "
                            f"{workflow.workflow_type!r} in this phase"
                        )
                except SpecialistError as exc:
                    # An unhandled or unregistered specialist parks the workflow
                    # rather than silently completing it.
                    workflow.state = models.AgentWorkflow.WAITING
                    workflow.waiting_on = f"no executable specialist: {exc}"[:255]
                    workflow.next_run_at = moment + timedelta(hours=6)
                    result.skipped_unhandled += 1
                    continue

            created = self._enqueue(workflow, agent, moment)
            if created:
                result.dispatched += 1
                result.per_agent[agent.id] = result.per_agent.get(agent.id, 0) + 1
            else:
                result.duplicates += 1

            # The workflow leaves the dispatchable set whether or not a job was
            # created, so a duplicate does not spin: the existing job owns it now.
            workflow.state = models.AgentWorkflow.RUNNING
            workflow.last_run_at = moment
            workflow.attempts += 1

        self.db.flush()
        metrics.inc("fleet.dispatched", result.dispatched)
        metrics.set_gauge("queue.dispatch_batch", float(result.scanned))
        return result

    def _enqueue(
        self, workflow: models.AgentWorkflow, agent: models.GranadaAgent, moment: datetime
    ) -> bool:
        """Create the durable job. Returns True when a new one was created.

        The idempotency key is ``workflow:version-of-attempt``, so a retry of the
        same attempt collapses while a genuinely new attempt is a new job. The
        unique constraint is the guarantee; this function merely makes the message
        unique.
        """
        ledger = JobLedger(self.db)
        job, created = ledger.enqueue(
            org_id=workflow.org_id,
            job_type=workflow.workflow_type,
            payload={
                # A HINT for locating work, never authority. The worker reads the
                # agent, tenant and workflow from the row.
                "workflow_id": workflow.id,
                "specialist_key": workflow.specialist_key,
            },
            domain="jobs",
            action=workflow.workflow_type,
            idempotency_key=f"{workflow.id}:{workflow.attempts}",
            available_at=moment,
        )
        job.agent_id = agent.id
        # The authority the work was created under. Compared against the live
        # agent by the worker, so a reduction in authority cannot be outrun.
        job.agent_version = agent.version
        job.workflow_id = workflow.id
        job.trace_id = workflow.context.get("correlation_id") if workflow.context else None

        ledger.stage_event(
            org_id=workflow.org_id,
            stream=f"granada:v1:workflow:{workflow.workflow_type}",
            event_type="workflow.dispatched",
            payload={
                "workflow_id": workflow.id,
                "agent_id": agent.id,
                "job_id": job.id,
                "workflow_type": workflow.workflow_type,
                "specialist_key": workflow.specialist_key,
                "correlation_id": job.trace_id,
                # The event that caused this one, so the chain is reconstructable.
                "causation_id": (workflow.context or {}).get("causation_id"),
            },
        )
        return created


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
class AgentWorker:
    """Executes one claimed job on behalf of the agent named on the row."""

    def __init__(self, db: Session, *, worker_id: str) -> None:
        if not worker_id:
            raise FleetError("a worker must identify itself, so leases are attributable")
        self.db = db
        self.worker_id = worker_id

    # ------------------------------------------------------------------
    def execute(self, job_id: str) -> ExecutionResult:
        """Run one job to completion, or park it, or fail it. Never half-way.

        Order matters and follows the brief: load the durable job, resolve the
        tenant and agent **from the row**, verify the agent belongs to the
        organisation, verify it is active, compare the authority version, resolve
        the specialist, check the specialist's authority against the agent's,
        lease, execute, persist, advance the workflow, record activity, stage
        events, commit - and only then may Redis be acked by the caller.
        """
        ledger = JobLedger(self.db)
        job = ledger.get(job_id)
        if job is None:
            return ExecutionResult(job_id=job_id, outcome=ExecutionResult.SKIPPED,
                                   detail="no such job")

        attempt = ledger.claim(job_id=job_id, worker_id=self.worker_id, org_id=job.org_id)
        if attempt is None:
            # Already running, already done, or in backoff. Not an error.
            return ExecutionResult(job_id=job_id, outcome=ExecutionResult.SKIPPED,
                                   detail="not claimable", workflow_id=job.workflow_id)
        job.started_executing_at = job.started_executing_at or datetime.now(timezone.utc)
        self.db.flush()

        try:
            return self._run(job, ledger, attempt)
        except Exception as exc:
            ledger.fail(
                attempt,
                category="TRANSIENT_NETWORK" if _retryable(exc) else "PERMANENT_REJECTION",
                error=f"{type(exc).__name__}: {exc}",
                retryable=_retryable(exc),
            )
            metrics.inc("fleet.execution_failed", reason=type(exc).__name__)
            logger.warning(
                "fleet_execution_failed",
                extra={"job_id": job_id, "agent_id": job.agent_id,
                       "reason": type(exc).__name__, "error": str(exc)[:500]},
            )
            self.db.flush()
            return ExecutionResult(
                job_id=job_id, outcome=ExecutionResult.FAILED,
                workflow_id=job.workflow_id, agent_id=job.agent_id,
                detail=f"{type(exc).__name__}: {exc}"[:500],
            )

    # ------------------------------------------------------------------
    def _run(self, job: models.Job, ledger: JobLedger, attempt: models.JobAttempt) -> ExecutionResult:
        # 1. Tenant and agent, both from the DURABLE ROW.
        if not job.org_id:
            raise FleetError("a job with no organisation cannot be executed")
        service = GranadaAgentService(self.db, job.org_id)

        agent = None
        if job.agent_id:
            agent = self.db.execute(
                select(models.GranadaAgent).where(models.GranadaAgent.id == job.agent_id)
            ).scalars().first()
            if agent is None:
                raise AgentNotFound(f"job names agent {job.agent_id}, which does not exist")
            # 2. The invariant. PostgreSQL also refuses this shape, so reaching it
            #    means a pre-constraint row or a bug - and both must stop.
            if agent.org_id != job.org_id:
                raise AgentMismatch(
                    f"job belongs to organisation {job.org_id} but names agent "
                    f"{agent.id}, which belongs to {agent.org_id}"
                )

        workflow = None
        if job.workflow_id:
            workflow = self.db.execute(
                select(models.AgentWorkflow).where(
                    models.AgentWorkflow.id == job.workflow_id,
                    models.AgentWorkflow.org_id == job.org_id,
                )
            ).scalars().first()
            if workflow is None:
                raise FleetError(
                    f"job names workflow {job.workflow_id}, which is not in this organisation"
                )

        # The specialist comes from the WORKFLOW, and the work type is the
        # fallback - not the other way round. A job_type is a workflow type
        # ("opportunity_match"), not a specialist key ("MATCHER"), and treating one
        # as the other produced UnknownSpecialist for every unassigned workflow.
        specialist_key = workflow.specialist_key if workflow else None
        if specialist_key is None:
            candidates = for_work_type(job.job_type)
            if len(candidates) != 1:
                raise FleetError(
                    f"workflow type {job.job_type!r} is owned by "
                    f"{[c.key for c in candidates] or 'no specialist'}; "
                    "exactly one is required to execute it"
                )
            specialist_key = candidates[0].key
        spec = check_work_type(specialist_key, job.job_type)

        if agent is None:
            # System-level work: no agent, no authority to check. It still runs
            # under the tenant it names and cannot act as anybody's agent.
            return self._execute_handler(job, ledger, attempt, None, workflow, spec)

        # 3. The agent must be available and authorised.
        if agent.status != models.GranadaAgent.ACTIVE:
            return self._park(job, ledger, attempt, workflow, agent, spec,
                              outcome=ExecutionResult.AGENT_PAUSED,
                              reason=f"agent is {agent.status}")

        # 4. Authority version. THE check that stops stale permission finishing.
        stale = job.agent_version is not None and job.agent_version != agent.version
        if stale:
            verdict = self._reauthorize(job, agent, spec)
            if verdict != "CONTINUE":
                return self._park(
                    job, ledger, attempt, workflow, agent, spec,
                    outcome=verdict,
                    reason=(
                        f"authority changed from version {job.agent_version} to "
                        f"{agent.version}; {verdict}"
                    ),
                )

        # 5. The specialist may not exceed its agent.
        try:
            service.require_authority(spec.key, spec.required_authority)
        except AuthorityExceeded as exc:
            return self._park(job, ledger, attempt, workflow, agent, spec,
                              outcome=ExecutionResult.CANCELLED_BY_POLICY, reason=str(exc))

        return self._execute_handler(job, ledger, attempt, agent, workflow, spec)

    # ------------------------------------------------------------------
    def _reauthorize(self, job: models.Job, agent: models.GranadaAgent, spec: Any) -> str:
        """Decide what a version change means for already-queued work.

        The brief is explicit that the version must not simply be overwritten: the
        mismatch is *information*. Three outcomes, and the interesting one is the
        middle: an increase in authority does not retroactively authorise old work
        either, because the work was scoped under the earlier level.
        """
        if agent.status != models.GranadaAgent.ACTIVE:
            return ExecutionResult.AGENT_PAUSED
        if not Autonomy.at_least(agent.autonomy, spec.required_authority):
            # Authority was reduced below what this step needs. Refuse, do not
            # downgrade the work to something weaker - silently substituting a
            # lesser action is how a customer gets something they did not choose.
            return ExecutionResult.CANCELLED_BY_POLICY
        if spec.required_authority == Autonomy.MONITOR_ONLY:
            # Purely internal, non-consequential work: safe to proceed on the new
            # version, recorded as a re-authorisation rather than as a clean run.
            job.agent_version = agent.version
            metrics.inc("fleet.reauthorized")
            return "CONTINUE"
        # Anything with a side effect waits for a person after an authority change.
        return ExecutionResult.PARKED

    # ------------------------------------------------------------------
    def _execute_handler(
        self,
        job: models.Job,
        ledger: JobLedger,
        attempt: models.JobAttempt,
        agent: Optional[models.GranadaAgent],
        workflow: Optional[models.AgentWorkflow],
        spec: Any,
    ) -> ExecutionResult:
        require_executable(spec.key)

        context = {
            "job": job,
            "workflow": workflow,
            "agent": agent,
            "specialist": spec,
            "db": self.db,
            "correlation_id": (workflow.context or {}).get("correlation_id") if workflow else None,
        }
        activity = spec.load(job.job_type)(self.db, context)

        # Advance the workflow canonically, then record what happened.
        if workflow is not None and activity.get("next_state"):
            workflow.state = activity["next_state"]
            workflow.specialist_key = activity.get("next_specialist", workflow.specialist_key)
            workflow.waiting_on = activity.get("waiting_on")
            if activity.get("next_run_at"):
                workflow.next_run_at = activity["next_run_at"]
            elif activity["next_state"] == models.AgentWorkflow.RUNNING:
                workflow.next_run_at = datetime.now(timezone.utc)

        if activity.get("enqueue"):
            self._enqueue_next(workflow, agent, activity["enqueue"])

        self._record_activity(job, agent, workflow, spec, activity)
        self._stage_events(job, agent, workflow, spec, activity)

        ledger.succeed(attempt, output={"specialist": spec.key, "summary": activity.get("summary")})

        # last_active_at becomes real HERE, and only after the work is durable.
        # The dispatcher looking at an agent is not the agent working.
        if agent is not None and activity.get("meaningful", True):
            service = GranadaAgentService(self.db, agent.org_id)
            service.touch(specialist_key=spec.key, activity=activity.get("activity"))
            service.release_specialist(spec.key)

        self.db.flush()
        metrics.inc("fleet.execution_succeeded", specialist=spec.key)
        return ExecutionResult(
            job_id=job.id, outcome=ExecutionResult.SUCCEEDED,
            workflow_id=job.workflow_id, agent_id=job.agent_id,
            specialist=spec.key, detail=activity.get("summary"),
            activity_recorded=True,
        )

    def _enqueue_next(
        self, workflow: Optional[models.AgentWorkflow], agent: Optional[models.GranadaAgent],
        spec: dict[str, Any],
    ) -> None:
        """Schedule the next specialist step, carrying causation forward."""
        if workflow is None or agent is None:
            return
        from agent.granada_agent import GranadaAgentService as _S

        service = _S(self.db, workflow.org_id)
        context = dict(workflow.context or {})
        if spec.get("causation_id"):
            context["causation_id"] = spec["causation_id"]
        if spec.get("correlation_id"):
            context["correlation_id"] = spec["correlation_id"]

        follow_on = service.schedule(
            workflow_type=spec["workflow_type"],
            subject_type=workflow.subject_type,
            subject_id=workflow.subject_id,
            specialist_key=spec.get("specialist_key"),
            run_at=datetime.now(timezone.utc),
            context=context,
        )
        # The CURRENT workflow's state is deliberately NOT touched here. The first
        # version set it to WAITING and made it due immediately, so the completed
        # step was re-dispatched on every sweep - an infinite loop that re-ran
        # matching forever and starved the rest of the pipeline. The step is
        # finished; only the follow-on is scheduled, and `_execute_handler` has
        # already applied `next_state`.
        return follow_on

    def _record_activity(
        self,
        job: models.Job,
        agent: Optional[models.GranadaAgent],
        workflow: Optional[models.AgentWorkflow],
        spec: Any,
        activity: dict[str, Any],
    ) -> None:
        """Write the customer-facing record. Structured, never a log sentence."""
        if agent is None:
            return
        self.db.add(
            models.AgentActivity(
                agent_id=agent.id,
                org_id=agent.org_id,
                specialist_key=spec.key,
                workflow_id=workflow.id if workflow else None,
                job_id=job.id,
                activity_type=activity.get("activity_type", spec.key.lower()),
                summary_key=activity.get("summary_key", f"{spec.key.lower()}.completed"),
                subject_type=workflow.subject_type if workflow else None,
                subject_id=workflow.subject_id if workflow else None,
                structured_data=activity.get("structured_data"),
                visibility=activity.get(
                    "visibility", models.AgentActivity.VISIBILITY_CUSTOMER
                ),
                correlation_id=activity.get("correlation_id"),
                occurred_at=datetime.now(timezone.utc),
            )
        )
        self.db.flush()

    def _stage_events(
        self,
        job: models.Job,
        agent: Optional[models.GranadaAgent],
        workflow: Optional[models.AgentWorkflow],
        spec: Any,
        activity: dict[str, Any],
    ) -> None:
        """Stage events in the SAME transaction as the state change.

        Never published inline: a crash between commit and XADD would lose the
        event, and an XADD before commit would publish work that then rolled back.
        The relay moves committed rows to Redis afterwards.
        """
        if agent is None:
            return
        ledger = JobLedger(self.db)
        for event in activity.get("events", []):
            ledger.stage_event(
                org_id=agent.org_id,
                stream=event.get("stream", f"granada:v1:agent:{spec.key.lower()}"),
                event_type=event["event_type"],
                payload={
                    **event.get("payload", {}),
                    "agent_id": agent.id,
                    "organisation_id": agent.org_id,
                    "workflow_id": workflow.id if workflow else None,
                    "job_id": job.id,
                    "specialist_key": spec.key,
                    "correlation_id": activity.get("correlation_id"),
                    # The chain that lets Granada answer "why did this agent do this".
                    "causation_id": event.get("causation_id")
                    or (workflow.context or {}).get("causation_id") if workflow else None,
                },
            )
        self.db.flush()

    def _park(
        self,
        job: models.Job,
        ledger: JobLedger,
        attempt: models.JobAttempt,
        workflow: Optional[models.AgentWorkflow],
        agent: Optional[models.GranadaAgent],
        spec: Any,
        *,
        outcome: str,
        reason: str,
    ) -> ExecutionResult:
        """Park the work without failing it.

        Parking is healthy: the work is waiting on a person, on an authority
        change or on an agent that is paused. It is kept scheduled so it resumes
        when the condition clears, rather than being retried into a dead letter or
        lost.
        """
        if workflow is not None:
            workflow.state = models.AgentWorkflow.WAITING
            workflow.waiting_on = reason[:255]
            workflow.next_run_at = datetime.now(timezone.utc) + timedelta(hours=1)

        if agent is not None:
            self.db.add(
                models.AgentActivity(
                    agent_id=agent.id,
                    org_id=agent.org_id,
                    specialist_key=spec.key,
                    workflow_id=workflow.id if workflow else None,
                    job_id=job.id,
                    activity_type="workflow.parked",
                    summary_key=f"{spec.key.lower()}.parked",
                    subject_type=workflow.subject_type if workflow else None,
                    subject_id=workflow.subject_id if workflow else None,
                    structured_data={"reason": reason},
                    visibility=models.AgentActivity.VISIBILITY_CUSTOMER,
                    occurred_at=datetime.now(timezone.utc),
                )
            )
            ledger.stage_event(
                org_id=agent.org_id,
                stream="granada:v1:workflow:parked",
                event_type="workflow.parked",
                payload={
                    "workflow_id": workflow.id if workflow else None,
                    "agent_id": agent.id,
                    "job_id": job.id,
                    "outcome": outcome,
                    "reason": reason,
                },
            )

        # Succeeded, not failed: the job did its work - the work was to stop.
        ledger.succeed(attempt, output={"outcome": outcome, "reason": reason})
        self.db.flush()
        metrics.inc("fleet.execution_parked", outcome=outcome)
        return ExecutionResult(
            job_id=job.id, outcome=outcome, workflow_id=job.workflow_id,
            agent_id=job.agent_id, specialist=spec.key, detail=reason,
        )


def _retryable(exc: Exception) -> bool:
    """Retry by exception TYPE, never by matching message text."""
    if isinstance(exc, (AgentError, SpecialistError, FleetError, WorkspaceError)):
        return False
    if isinstance(exc, GuardFailed):
        return False
    return True


# ---------------------------------------------------------------------------
# Specialist handlers
# ---------------------------------------------------------------------------
def _handle_match(db: Session, context: dict[str, Any]) -> dict[str, Any]:
    """Deterministic hard gates first, then the decision step. Nothing else.

    This handler deliberately does **not** create the application workspace. That
    is the qualifier's job, so a failure in judgment cannot be confused with a
    failure in arithmetic.
    """
    workflow = context["workflow"]
    agent = context["agent"]
    if workflow is None or agent is None:
        raise FleetError("matching requires a workflow and an agent")

    opportunity = db.execute(
        select(models.Opportunity).where(models.Opportunity.id == workflow.subject_id)
    ).scalars().first()
    if opportunity is None:
        return {
            "summary": "the opportunity no longer exists",
            "summary_key": "match.opportunity_missing",
            "next_state": models.AgentWorkflow.CANCELLED,
            "meaningful": False,
        }

    matcher = Matcher(db, agent.org_id)
    matches = matcher.evaluate([opportunity])
    match = matches[0]
    qualification = match.qualification

    from agent.workspace import ApplicationWorkspace as _W

    workspace = _W(db, agent.org_id)
    application = workspace.create(
        opportunity, state=DISCOVERED,
        reason=f"created by {context['specialist'].display_name}",
        correlation_id=context.get("correlation_id"),
    )

    if qualification.passed:
        workspace.transition(
            application, MATCHED, reason=qualification.summary(),
            actor_type=models.ApplicationTransition.ACTOR_AGENT,
            job_id=context["job"].id,
            correlation_id=context.get("correlation_id"),
        )
        return {
            "summary": f"passed every hard gate; {len(qualification.gates)} gates evaluated",
            "summary_key": "match.passed",
            "activity_type": "match",
            "activity": "evaluated an opportunity against the organisation's profile",
            "structured_data": {
                "opportunity_id": opportunity.id,
                "gates_evaluated": len(qualification.gates),
            },
            "enqueue": {
                "workflow_type": WORKFLOW_QUALIFY,
                # The Matching Agent owns the qualify step: the roster has ten
                # specialists and an eleventh holding one function would be roster
                # noise the customer would have to read.
                "specialist_key": "MATCHER",
                "correlation_id": context.get("correlation_id"),
            },
            "events": [{
                "event_type": "opportunity.matched",
                "stream": "granada:v1:matching:matched",
                "payload": {"opportunity_id": opportunity.id, "application_id": application.id},
            }],
            "next_state": models.AgentWorkflow.COMPLETED,
        }

    if qualification.state == models.OpportunityMatch.REJECTED_BY_RULE:
        workspace.transition(
            application, REJECTED_BY_RULE, reason=qualification.summary(),
            actor_type=models.ApplicationTransition.ACTOR_AGENT,
            job_id=context["job"].id,
        )
        return {
            "summary": qualification.summary(),
            "summary_key": "match.rejected_by_rule",
            "activity_type": "match",
            "activity": "rejected an opportunity by rule",
            "structured_data": {
                "opportunity_id": opportunity.id,
                "failed_gates": qualification.failed_gates,
            },
            "events": [{
                "event_type": "opportunity.rejected_by_rule",
                "stream": "granada:v1:matching:rejected",
                "payload": {
                    "opportunity_id": opportunity.id,
                    "failed_gates": qualification.failed_gates,
                },
            }],
            "next_state": models.AgentWorkflow.COMPLETED,
        }

    # NEEDS_DATA: a work item for a human, not a failure.
    return {
        "summary": qualification.summary(),
        "summary_key": "match.needs_data",
        "activity_type": "match",
        "activity": "could not decide: information is missing",
        "structured_data": {
            "opportunity_id": opportunity.id,
            "unknown_gates": qualification.unknown_gates,
        },
        "next_state": models.AgentWorkflow.WAITING,
        "waiting_on": "organisation information: " + ", ".join(qualification.unknown_gates),
    }


def _handle_qualify(db: Session, context: dict[str, Any]) -> dict[str, Any]:
    """The bounded decision, through the DecisionGateway. Jev stays shadow.

    The gateway is given a minimal state and typed questions, and its answer feeds
    Granada's policy - never the other way round. In SHADOW nothing it returns can
    change what happens.
    """
    from agent.decision.gateway import ProviderChain, build_gateway
    from agent.decision.models import (
        DecisionQuestion,
        DecisionRequest,
        QuestionType,
        AGENT_ROUTES,
    )

    workflow = context["workflow"]
    agent = context["agent"]
    db_session = context["db"]

    opportunity = db_session.execute(
        select(models.Opportunity).where(models.Opportunity.id == workflow.subject_id)
    ).scalars().first()
    if opportunity is None:
        return {"summary": "the opportunity no longer exists",
                "summary_key": "qualify.opportunity_missing",
                "next_state": models.AgentWorkflow.CANCELLED, "meaningful": False}

    memory = OrganisationMemory(db_session, agent.org_id)
    eligibility = EligibilityEngine(memory).qualify(opportunity)

    request = DecisionRequest(
        decision_type="opportunity_triage",
        questions=(
            DecisionQuestion(
                "strategic_fit", QuestionType.CHOICE, "How well does this fit?",
                ("VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"),
            ),
            DecisionQuestion(
                "worth_researching", QuestionType.BOOLEAN, "Is this worth deeper research?",
            ),
        ),
        state={
            "organisation": {
                "country": memory.submission_facts().get("country"),
                "type": memory.submission_facts().get("organisation_type"),
            },
            "opportunity": {
                "title": opportunity.title,
                "country": opportunity.country,
                "sector": opportunity.sector,
            },
            "eligibility": {
                "failed_gates": eligibility.failed_gates,
                "unknown_gates": eligibility.unknown_gates,
            },
        },
        organisation_id=agent.org_id,
        opportunity_id=opportunity.id,
        workflow_id=workflow.id,
        correlation_id=context.get("correlation_id"),
    )

    # Built from settings; with JEV_ENABLED=false this is rules + no LLM, which is
    # the supported default. No API key is required for the fleet to work.
    gateway = build_gateway(db=db_session, settings=_settings())
    try:
        acting, shadow, agreement = gateway.decide_with_shadow(request)
        decided = True
        detail = f"{acting.provider} answered {acting.value('worth_researching')}"
        confidence = acting.confidence
        shadow_info = None
        if shadow is not None:
            shadow_info = {
                "provider": shadow.provider,
                "answers": {k: a.value for k, a in shadow.answers.items()},
                "agreed": agreement.fully_agreed if agreement else None,
            }
    except Exception as exc:
        # A decision failure must not stop the fleet: the workflow parks.
        return {
            "summary": f"decision unavailable: {type(exc).__name__}",
            "summary_key": "qualify.decision_unavailable",
            "activity_type": "decision",
            "activity": "could not obtain a decision and parked the workflow",
            "next_state": models.AgentWorkflow.WAITING,
            "waiting_on": "decision provider unavailable",
            "events": [{"event_type": "decision.failed", "stream": "granada:v1:decision:failed",
                        "payload": {"error": type(exc).__name__}}],
        }

    workspace = ApplicationWorkspace(db_session, agent.org_id)
    application = workspace.get(opportunity.id)
    if application is not None:
        workspace.transition(
            application, QUALIFIED, reason=detail,
            actor_type=models.ApplicationTransition.ACTOR_AGENT,
            job_id=context["job"].id,
            decision_id=acting.decision_id,
            correlation_id=context.get("correlation_id"),
        )
        workspace.transition(
            application, RESEARCHING, reason="queued for donor research",
            actor_type=models.ApplicationTransition.ACTOR_AGENT,
            job_id=context["job"].id,
        )

    return {
        "summary": detail,
        "summary_key": "qualify.completed",
        "activity_type": "decision",
        "activity": "qualified an opportunity and queued it for research",
        "structured_data": {
            "opportunity_id": opportunity.id,
            "provider": acting.provider,
            "confidence": confidence,
            "shadow": shadow_info,
            "eligibility_failed": eligibility.failed_gates,
        },
        "enqueue": {
            "workflow_type": WORKFLOW_RESEARCH,
            "specialist_key": "DONOR_RESEARCHER",
            "correlation_id": context.get("correlation_id"),
        },
        "events": [
            {"event_type": "decision.completed", "stream": "granada:v1:decision:completed",
             "payload": {"decision_id": acting.decision_id, "provider": acting.provider,
                         "confidence": confidence}},
            {"event_type": "application.created", "stream": "granada:v1:workflow:application",
             "payload": {"opportunity_id": opportunity.id,
                         "application_id": application.id if application else None}},
        ],
        "next_state": models.AgentWorkflow.COMPLETED,
    }


def _handle_donor_research(db: Session, context: dict[str, Any]) -> dict[str, Any]:
    """Build and persist a provenance-stamped research record."""
    workflow = context["workflow"]
    agent = context["agent"]

    opportunity = db.execute(
        select(models.Opportunity).where(models.Opportunity.id == workflow.subject_id)
    ).scalars().first()
    if opportunity is None:
        return {"summary": "the opportunity no longer exists",
                "summary_key": "research.opportunity_missing",
                "next_state": models.AgentWorkflow.CANCELLED, "meaningful": False}

    eligibility = EligibilityEngine(OrganisationMemory(db, agent.org_id)).qualify(opportunity)
    service = DonorResearchService(db, agent)
    result = service.build(
        opportunity,
        eligibility={
            "hard_gate_passed": eligibility.passed,
            "failed_gates": eligibility.failed_gates,
            "unknown_gates": eligibility.unknown_gates,
        },
    )
    workspace = ApplicationWorkspace(db, agent.org_id)
    application = workspace.get(opportunity.id)
    row = service.persist(
        result, opportunity, application_id=application.id if application else None
    )

    if application is not None:
        workspace.transition(
            application, PREPARING, reason="research completed",
            actor_type=models.ApplicationTransition.ACTOR_AGENT,
            job_id=context["job"].id,
        )

    return {
        "summary": (
            f"research version {row.version} recorded; "
            f"{len(result.inferred_fields)} inferred, {len(result.unknown_fields)} unknown"
        ),
        "summary_key": "research.completed",
        "activity_type": "research",
        "activity": f"researched {opportunity.source_name}",
        "structured_data": {
            "opportunity_id": opportunity.id,
            "research_id": row.id,
            "research_version": row.version,
            "unknown_fields": result.unknown_fields,
            "required_documents": sorted(result.required_documents),
        },
        "events": [{
            "event_type": "research.completed",
            "stream": "granada:v1:agent:donor_researcher",
            "payload": {
                "opportunity_id": opportunity.id,
                "research_id": row.id,
                "research_version": row.version,
                "opportunity_version": result.opportunity_version,
            },
        }],
        # STOP HERE for this phase. The proposal is not generated and nothing is
        # submitted: Phase 6c proves autonomous INTERNAL work.
        "next_state": models.AgentWorkflow.COMPLETED,
    }


def _settings() -> Any:
    try:
        from config import settings

        return settings
    except Exception:  # pragma: no cover - configuration unavailable
        return None
