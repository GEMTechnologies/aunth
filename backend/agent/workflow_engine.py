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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

from sqlalchemy import func, select, text
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


#: How long a workflow waits after parking on missing INFORMATION rather than on a transient fault.
#:
#: Six hours, matching the unhandled-specialist path, because the blocker is a human action - an
#: organisation supplying its country or registration date - and retrying every few seconds cannot
#: make that happen any sooner.
WAITING_RETRY_BACKOFF = timedelta(hours=6)

#: Workflow types this phase can actually execute.
WORKFLOW_MATCH = "opportunity_match"
WORKFLOW_QUALIFY = "opportunity_qualify"
WORKFLOW_RESEARCH = "donor_research"
# Generates the documents a listing requires from the organisation's VERIFIED facts.
WORKFLOW_DOCUMENT = "document_generate"
# Assembles the prepared documents into the frozen package a human authorises.
WORKFLOW_ASSEMBLE = "application_assemble"

#: Section 6 of the multimodal directive: run the browser against an assembled package as a LEASED
#: capability. Disabled by default - the handler refuses unless the flag is on, so registering the
#: workflow does not enable it.
WORKFLOW_BROWSER_TASK = "browser_task"
#: Phase 7a. Email is one more wake condition for the same shared fleet — there is
#: no per-mailbox worker and no mail daemon. Note the absence of a send work type:
#: `email_send` is registered on the roster and owns no handler, so it cannot be
#: dispatched at all.
WORKFLOW_MAIL = "mail_process"
#: Phase 7b. `mail_send` executes an intent a HUMAN already approved; it does not
#: decide to send. An intent without a live matching approval is refused by the
#: final authority check, so the work type being dispatchable does not make
#: autonomous sending possible.
WORKFLOW_MAIL_SEND = "mail_send"
WORKFLOW_MAIL_RECONCILE = "mail_reconcile"
WORKFLOW_MAIL_SYNC = "mail_sync"

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
    #: Workflows the discovery pass created this sweep. Reported separately from dispatched,
    #: because "I found 25 new things to do" and "I ran 25 things" are different facts, and an
    #: operator watching one would be misled by the other.
    discovered: int = 0
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
def _truthy(value: Optional[str], *, default: bool = False) -> bool:
    """Interpret a configuration string. Absent means the default; a RECOGNISED false word means False.

    `bool(os.environ.get(...))` is the classic defect here: the string "0" is truthy, so
    `FLEET_NARROW_CLAIM=0` would have ENABLED the cutover. A flag whose off value turns it on is worse
    than no flag.
    """
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class FleetDispatcher:
    """Discovers due work across every agent and enqueues it. Nothing else."""

    def __init__(
        self,
        db: Session,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        per_agent_limit: int = DEFAULT_PER_AGENT_LIMIT,
        use_narrow_claim: Optional[bool] = None,
        use_tenant_binding: Optional[bool] = None,
    ) -> None:
        """`use_narrow_claim` defaults to the class attribute, which is False, and to the environment
        when one is set.

        THE ENVIRONMENT IS READ HERE RATHER THAN AT IMPORT. A module-level read would freeze the value
        at import time, so a test that sets the variable would not see it and a rolling restart could
        not change it - and a deployment flag you cannot change by deploying is not a flag.

        Precedence is explicit argument > environment > class default, because that is the order a
        caller would expect: the most local, most explicit statement wins.
        """
        self.db = db
        self.batch_size = batch_size
        self.per_agent_limit = per_agent_limit
        if use_narrow_claim is None:
            import os

            use_narrow_claim = _truthy(
                os.environ.get("FLEET_NARROW_CLAIM"), default=type(self).USE_NARROW_CLAIM
            )
        self.use_narrow_claim = use_narrow_claim
        if use_tenant_binding is None:
            import os

            use_tenant_binding = _truthy(
                os.environ.get("FLEET_TENANT_BINDING"), default=type(self).USE_TENANT_BINDING
            )
        self.use_tenant_binding = use_tenant_binding

    #: Whether to bind the connection to ONE organisation at a time instead of relying on
    #: `granada_fleet`'s BYPASSRLS (ADR-0011 step 2).
    #:
    #: FALSE BY DEFAULT and deliberately additive, for the same reason `USE_NARROW_CLAIM` is: the
    #: dispatcher is serving production, and switching a default in the commit that introduces the
    #: option makes a regression indistinguishable from the intended change.
    #:
    #: WHEN BOTH ARE ON the dispatcher stops needing BYPASSRLS at all:
    #:
    #:   * candidate selection comes from `fleet_due_workflow_refs` (SECURITY DEFINER, ids + org only)
    #:   * every tenant-scoped statement runs bound to `app.current_org_id`
    #:
    #: Turning this on is a prerequisite for revoking the privilege, never a substitute for testing it.
    USE_TENANT_BINDING = False

    def _bind_tenant(self, org_id: Optional[str]) -> None:
        """Bind this connection to one organisation, or clear the binding with ``None``.

        `set_config(..., false)` is SESSION-level, not transaction-local, and that is deliberate. The
        dispatcher commits inside a sweep, and a transaction-local setting is discarded at COMMIT -
        so the second half of a sweep would run unbound and return zero rows while looking like a
        quiet fleet. Session scope plus an explicit clear in `tenant_scope`'s `finally` is the
        predictable option here.
        """
        self.db.execute(
            text("SELECT set_config('app.current_org_id', :org, false)"),
            {"org": org_id or ""},
        )

    @contextmanager
    def tenant_scope(self, org_id: Optional[str]) -> Iterator[None]:
        """Run tenant-scoped work bound to one organisation, then clear the binding.

        A NO-OP WHEN BINDING IS OFF, and that is not an optimisation. The dispatcher wraps its loop
        body unconditionally, so without this guard the default path would issue a `set_config` per
        workflow - which fails outright on SQLite, where the function does not exist. The suite is
        SQLite; the fleet is PostgreSQL. A guard here keeps the default path byte-for-byte the
        behaviour every existing test already exercises.

        The `finally` matters more than the bind. A dispatcher that binds and then raises would leave
        the NEXT organisation's work running under the previous organisation's context - which is the
        one failure mode in this design that could cross tenants rather than merely return nothing.
        """
        if not self.use_tenant_binding:
            yield
            return

        self._bind_tenant(org_id)
        try:
            yield
        finally:
            self._bind_tenant(None)

    def due_workflow_refs(self, *, limit: Optional[int] = None) -> list[tuple[str, Optional[str]]]:
        """Due work as ``(workflow_id, org_id)`` pairs, from the narrow SECURITY DEFINER function.

        WHY REFS RATHER THAN ROWS

        The ids this returns belong to many organisations. There is no single value of
        `app.current_org_id` that lets a follow-up `WHERE id IN (...)` re-select them once
        `granada_fleet` has no BYPASSRLS - bound to one organisation it returns that organisation's
        rows only and the fleet dispatches one tenant per sweep; bound to none it returns zero.

        So the dispatcher does not re-select across tenants at all. It takes the refs and processes
        ONE WORKFLOW AT A TIME, each bound to its own organisation - which is why the refs must carry
        `org_id`. That is ADR-0011 step 2 in one sentence.
        """
        rows = self.db.execute(
            text(
                "SELECT workflow_id, org_id FROM fleet_due_workflow_refs(:batch, :per_agent)"
            ),
            {"batch": limit or self.batch_size, "per_agent": self.per_agent_limit},
        )
        return [(row[0], row[1]) for row in rows]

    #: Whether to discover due work through the narrow SECURITY DEFINER function instead of reading
    #: `agent_workflows` directly under `granada_fleet`'s BYPASSRLS (ADR-0011).
    #:
    #: FALSE BY DEFAULT, and deliberately additive. This class serves the fleet today and the code it
    #: replaces is the code every existing test exercises, so switching the default here would change
    #: production behaviour in the same commit that introduces the option - and a regression would be
    #: indistinguishable from the intended change. Setting it true is the CUTOVER, and it is a separate,
    #: reversible decision made by configuration.
    USE_NARROW_CLAIM = False

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

        ADR-0011: when ``USE_NARROW_CLAIM`` is set, the candidate ids come from
        ``fleet_due_workflow_ids`` - a SECURITY DEFINER function that performs the same partition
        inside the database and returns IDS ONLY. The privilege then belongs to one reviewed query
        rather than to the whole connection, which is what `granada_fleet`'s BYPASSRLS currently gives
        the dispatcher for every statement it runs.

        The two paths are equivalent by construction AND by test
        (`tests/test_adr0011_narrowing.py`), because a narrowed claim that returned a different
        candidate set would silently change which organisations get dispatched.
        """
        moment = now or datetime.now(timezone.utc)

        if self.use_narrow_claim:
            # IDS from the function, rows from the ORM. The function returns ids only, so this path
            # cannot be used to read a column the caller is not entitled to - the re-select below is
            # still subject to the caller's own row policies.
            ids = [
                row[0]
                for row in self.db.execute(
                    text(
                        "SELECT workflow_id FROM fleet_due_workflow_ids(:batch, :per_agent)"
                    ),
                    {"batch": limit or self.batch_size, "per_agent": self.per_agent_limit},
                )
            ]
            if not ids:
                return []
            rows = list(
                self.db.execute(
                    select(models.AgentWorkflow).where(models.AgentWorkflow.id.in_(ids))
                ).scalars()
            )
            # ORDER IN PYTHON, by the same keys the SQL orders by. `IN (...)` does not preserve the
            # function's ordering, and a different order is a different dispatch sequence - the
            # trailing id is part of the contract, not a nicety.
            rows.sort(key=lambda w: (w.priority, w.next_run_at, w.id))
            return rows

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

    #: How many unevaluated opportunities one agent may pick up per sweep. Bounded for the same
    #: reason everything else here is: a catalogue of 100,000 opportunities must not turn one sweep
    #: into 100,000 workflow rows, and an agent should work steadily rather than in one avalanche.
    DISCOVERY_LIMIT_PER_AGENT = 25

    def discover_opportunity_work(self, *, now: Optional[datetime] = None) -> int:
        """Schedule a match workflow for catalogue opportunities this agent has not yet evaluated.

        WHY THIS EXISTS
        ---------------
        Nothing created the FIRST workflow. ``GranadaAgentService.schedule()`` is the idempotent
        entry point, ``dispatch_once`` claims what is already due, and **no route, hook or sweep ever
        called it**. So `agent_workflows` stayed empty, `due_workflows` returned nothing, and
        `dispatched=0` was read as "the fleet is idle" rather than "the fleet has never been given
        anything to do".

        Found on the VPS alongside the RLS blindness in ADR-0011. The two are separate defects and
        both had to be fixed: with only the credential, the dispatcher sees an empty table; with only
        discovery, it still cannot see what discovery wrote.

        WHAT IT DOES NOT DO
        -------------------
        It does not match anything. It schedules the work and records who owns it, so the matching
        stays in the handler where `matcher.evaluate()` and its gates live. A dispatcher that
        decided eligibility would be a dispatcher with an opinion about funding.
        """
        moment = now or datetime.now(timezone.utc)
        scheduled = 0

        # THE ROSTER IS THE ONE LEGITIMATE CROSS-TENANT READ. "Which organisations are active" cannot be
        # answered from inside any single tenant, because the answer IS the list of tenants. Everything
        # below it is tenant-scoped and must run bound.
        #
        # With binding off, the roster is a plain unscoped SELECT - which works only because the fleet
        # connection carries BYPASSRLS. With binding on, it comes from `fleet_active_agent_ids()`, whose
        # whole reason for existing is to be the smallest possible answer to that one question.
        if self.use_tenant_binding:
            roster = [
                (row[0], row[1])
                for row in self.db.execute(
                    text("SELECT agent_id, org_id FROM fleet_active_agent_ids()")
                )
            ]
        else:
            roster = [
                (a.id, a.org_id)
                for a in self.db.execute(
                    select(models.GranadaAgent).where(
                        models.GranadaAgent.status == models.GranadaAgent.ACTIVE
                    )
                ).scalars().all()
            ]
        if not roster:
            return 0

        for agent_id, org_id in roster:
            # EACH AGENT'S WORK RUNS UNDER ITS OWN ORGANISATION. The lookup, the `NOT IN` subqueries and
            # `GranadaAgentService.schedule()` all touch tenant tables, so without the binding each
            # returns nothing and discovery reports a quiet catalogue instead of a blind one.
            with self.tenant_scope(org_id):
                agent = self.db.execute(
                    select(models.GranadaAgent).where(models.GranadaAgent.id == agent_id)
                ).scalars().first()
                if agent is None:
                    continue

                # Opportunities this organisation has already evaluated. A match row means the work is
                # done; a workflow row means it is already queued, and re-scheduling would wake a
                # completed one or churn a queued one every sweep.
                evaluated = select(models.OpportunityMatch.opportunity_id).where(
                    models.OpportunityMatch.org_id == agent.org_id
                )
                queued = select(models.AgentWorkflow.subject_id).where(
                    models.AgentWorkflow.agent_id == agent.id,
                    models.AgentWorkflow.workflow_type == WORKFLOW_MATCH,
                    models.AgentWorkflow.subject_type == models.AgentWorkflow.SUBJECT_OPPORTUNITY,
                )

                candidates = self.db.execute(
                    select(models.Opportunity)
                    .where(
                        models.Opportunity.id.notin_(evaluated),
                        models.Opportunity.id.notin_(queued),
                        models.Opportunity.is_active.is_(True),
                    )
                    .order_by(models.Opportunity.created_at.desc())
                    .limit(self.DISCOVERY_LIMIT_PER_AGENT)
                ).scalars().all()

                if not candidates:
                    continue

                # Imported here rather than at module scope: granada_agent imports this module, so a
                # top-level import would be circular.
                from agent.granada_agent import GranadaAgentService

                service = GranadaAgentService(self.db, agent.org_id)
                for opportunity in candidates:
                    service.schedule(
                        workflow_type=WORKFLOW_MATCH,
                        subject_type=models.AgentWorkflow.SUBJECT_OPPORTUNITY,
                        subject_id=str(opportunity.id),
                        run_at=moment,
                    )
                    scheduled += 1

                # FLUSH INSIDE THE SCOPE, for the same reason `dispatch_once` does: a flush that
                # happens after the binding is cleared writes with `app.current_org_id` empty, matches
                # zero rows under RLS, and SQLAlchemy raises StaleDataError. The scheduler's writes are
                # subject to the same policy as the dispatcher's.
                self.db.flush()

        if scheduled:
            metrics.inc("fleet.discovered_opportunity_work", scheduled)
        return scheduled

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

        # DISCOVERY IS NOT HERE. This method dispatches work that is already due; discovering work is
        # a different job and it lives in `FleetRunner.sweep_once`, which sweeps. Putting it here
        # silently redefined what `dispatch_once` does - 36 tests that set up exactly the work they
        # wanted dispatched started finding extra rows. A method called `dispatch_once` should
        # dispatch (ADR-0011).
        try:
            if self.use_tenant_binding:
                # ADR-0011 step 2. Candidate selection crosses tenants by design, so it comes from the
                # SECURITY DEFINER function as `(workflow_id, org_id)` pairs. Everything after that runs
                # bound to one organisation, which is what lets `granada_fleet` keep working with
                # BYPASSRLS REVOKED.
                refs = self.due_workflow_refs(limit=limit)
            else:
                refs = [
                    (w.id, w.org_id) for w in self.due_workflows(now=moment, limit=limit)
                ]
        except Exception:
            # A dispatcher that dies must not poison the fleet.
            self.db.rollback()
            raise

        for workflow_id, org_id in refs:
            result.scanned += 1

            # THE BINDING IS PER WORKFLOW, and it wraps the row load itself. `granada_agents` and
            # `agent_workflows` are both under FORCE ROW LEVEL SECURITY, so the lookup below returns
            # None - read as "agent missing" and counted as `skipped_unhandled` - unless the tenant is
            # bound first. A silent skip is exactly how a blind dispatcher looks healthy.
            with self.tenant_scope(org_id):
                workflow = self.db.execute(
                    select(models.AgentWorkflow).where(models.AgentWorkflow.id == workflow_id)
                ).scalars().first()
                if workflow is None:
                    # Bound to its own organisation and still not visible: the row moved or was
                    # deleted since selection. Counted, not swallowed.
                    result.skipped_unhandled += 1
                    continue

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

                # FLUSH INSIDE THE SCOPE. THIS IS LOAD-BEARING, and the first version got it wrong.
                #
                # SQLAlchemy flushes pending changes when asked - and the method ended with a single
                # `self.db.flush()` AFTER the loop, by which point every `tenant_scope` had cleared the
                # binding. The UPDATE then ran with `app.current_org_id` empty, matched zero rows under
                # RLS, and SQLAlchemy raised:
                #
                #     StaleDataError: UPDATE statement on table 'agent_workflows' expected to
                #     update 1 row(s); 0 were matched.
                #
                # Seen in production within seconds of revoking BYPASSRLS, then rolled back. The SELECT
                # had succeeded - the row was loaded under the binding - so the fault was specific to
                # the WRITE path, which is exactly the path a candidate-set equivalence check does not
                # cover. Flushing per workflow is also better on its own terms: one row's failure no
                # longer abandons the rows before it in the same flush.
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
            elif activity["next_state"] == models.AgentWorkflow.WAITING:
                # WAITING HAD NO BRANCH HERE, and that made a parked workflow immortal.
                #
                # `due_workflows` selects `state IN (PENDING, WAITING) AND next_run_at <= now`. A
                # handler that parks on missing information - an organisation that has not supplied
                # its country or registration date - left `next_run_at` at the value it already had,
                # which is in the past the instant the row is written. So the very next sweep found
                # it due, dispatched another job, the handler parked it again, and the loop repeated
                # for as long as the platform ran.
                #
                # Measured on the VPS: 1,146 jobs from 2 workflows, all SUCCEEDED, all attempt=1 -
                # not retries, NEW jobs - accruing at roughly eight a minute, indefinitely. No error
                # was ever logged, because nothing failed. The only symptom was a table growing.
                #
                # The backoff is for the information, not for the failure: the gate is correct to
                # refuse, and a human has to supply the facts.
                workflow.next_run_at = datetime.now(timezone.utc) + WAITING_RETRY_BACKOFF

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
    """The bounded decision, through the DecisionGateway. The decision is recorded.

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

    # Built from settings; by default this is rules + local + no LLM, which is
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
        # NO LONGER THE END OF THE CHAIN.
        #
        # This used to stop here, and the comment said so: "the proposal is not generated and nothing
        # is submitted". That was honest while no Document Agent existed. One does now, and research
        # has just established `required_documents` above - so the next step is to prepare them, and
        # the pipeline can continue without a human scheduling it.
        #
        # The chain is now: match -> qualify -> research -> documents, each step enqueuing the next.
        # Submission is still deliberately absent: it stays behind the approval gate.
        "enqueue": {
            "workflow_type": WORKFLOW_DOCUMENT,
            "specialist_key": "DOCUMENT",
            "correlation_id": context.get("correlation_id"),
        },
        "next_state": models.AgentWorkflow.COMPLETED,
    }


def _handle_package_assemble(db: Session, context: dict[str, Any]) -> dict[str, Any]:
    """Assemble the submission package for an application, or report what it still needs.

    WHAT THIS IS. `SubmissionPackage` has existed since Phase 8 with `package_fingerprint`,
    `manifest`, `idempotency_key` and `handoff_ready_at`, and nothing ever created one - the table
    describing the thing a funder receives sat empty while the pipeline reached prepared documents
    and stopped. This is the assembler, and it reuses that table rather than adding another.

    MODE_HANDOFF. Everything is prepared for a person to submit in the funder's own portal. No
    external action is taken here, and nothing may claim otherwise.

    A MISSING CERTIFICATE IS NOT A CRASH. When the organisation has not supplied evidence, the
    package is NEEDS_DATA and the message names the documents. That is an outstanding action for a
    human, not an infrastructure failure, and the two must never be reported as one.

    IDEMPOTENT BY FINGERPRINT. Re-running with unchanged inputs reuses the existing package; a
    changed document produces a different fingerprint and therefore a new revision, because the
    authorisation a human gave applied to the previous set and does not transfer.
    """
    from agent import packaging
    from agent.document_types import required_types_for_application
    from agent.organisation_memory import DocumentVault
    from agent.workspace import ApplicationWorkspace

    workflow = context["workflow"]
    agent = context["agent"]
    if workflow is None or agent is None:
        raise FleetError("package assembly requires a workflow and an agent")

    opportunity = db.execute(
        select(models.Opportunity).where(models.Opportunity.id == workflow.subject_id)
    ).scalars().first()
    if opportunity is None:
        return {
            "summary": "the opportunity no longer exists",
            "summary_key": "package.opportunity_missing",
            "next_state": models.AgentWorkflow.CANCELLED,
            "meaningful": False,
        }

    organisation = db.execute(
        select(models.Organisation).where(models.Organisation.id == agent.org_id)
    ).scalars().first()
    if organisation is None:
        raise FleetError("package assembly requires the organisation row")

    workspace = ApplicationWorkspace(db, agent.org_id)
    application = workspace.get(str(opportunity.id))
    if application is None:
        # No application means matching never produced one for this opportunity. That is a pipeline
        # ordering problem, not a package problem, and it should say so rather than assemble an
        # empty package.
        return {
            "summary": "no application exists for this opportunity yet",
            "summary_key": "package.no_application",
            "next_state": models.AgentWorkflow.CANCELLED,
            "meaningful": False,
        }

    # Which documents does THIS listing ask for? The same canonicaliser the readiness gate uses, so
    # the assembler and the gate cannot disagree about what is required.
    listing_text = " ".join(
        str(part or "")
        for part in (opportunity.title, opportunity.description, opportunity.eligibility_criteria)
    )
    # ONE vocabulary: the same function the drift detector calls. They previously disagreed, and
    # every package reported drift as a result.
    required = required_types_for_application(listing_text)

    # ONLY USABLE DOCUMENTS. `usable()` returns current, APPROVED, unexpired versions - the gap
    # between uploading and approving is where the wrong document gets attached to a real
    # application, and it is already enforced there rather than here.
    vault = DocumentVault(db, agent.org_id)
    documents = vault.usable(scope_ref=str(opportunity.id))
    if not documents:
        documents = vault.usable()

    result = packaging.assemble(
        db,
        org_id=agent.org_id,
        agent_id=str(agent.id),
        application=application,
        opportunity=opportunity,
        organisation=organisation,
        documents=documents,
        required_types=required,
    )

    message = packaging.operator_message(result)

    # Advance the application workspace. A package that needs data moves the application to
    # WAITING_FOR_DATA, so a human surface can show why it is stopped without reading job tables.
    try:
        if result.status == models.SubmissionPackage.NEEDS_DATA:
            workspace.transition(
                application, WAITING_FOR_DATA, reason=message[:500],
                actor_type=models.ApplicationTransition.ACTOR_AGENT, job_id=context["job"].id,
            )
        elif result.status in (models.SubmissionPackage.DRAFT,
                               models.SubmissionPackage.AWAITING_AUTHORISATION):
            workspace.transition(
                application, PREPARING, reason=message[:500],
                actor_type=models.ApplicationTransition.ACTOR_AGENT, job_id=context["job"].id,
            )
    except Exception:  # noqa: BLE001 - a transition refusal must not lose the package
        logger.warning("package.transition_refused", extra={"opportunity_id": str(opportunity.id)})

    structured = {
        "opportunity_id": str(opportunity.id),
        "package_fingerprint": result.fingerprint,
        "status": result.status,
        "included": len(result.included),
        "missing": result.missing,
        "needs_data": result.needs_data,
        "reused": result.reused,
    }

    if result.status == models.SubmissionPackage.NEEDS_DATA:
        return {
            "summary": message[:1000],
            "summary_key": "package.needs_data",
            # PARK on the organisation, with the backoff the WAITING branch applies. This is the
            # defect fixed earlier in this project: a workflow waiting on a human must not be
            # re-dispatched on every sweep.
            "next_state": models.AgentWorkflow.WAITING,
            "waiting_on": message[:255],
            "meaningful": True,
            "activity_type": "package",
            "activity": "could not complete the application package",
            "structured_data": structured,
        }

    return {
        "summary": message[:1000],
        "summary_key": "package.assembled",
        "next_state": models.AgentWorkflow.COMPLETED,
        "meaningful": True,
        "activity_type": "package",
        "activity": "assembled an application package for review",
        "structured_data": structured,
        "events": [{
            "event_type": "package.assembled",
            "stream": "granada:v1:workflow:package",
            "payload": {
                "opportunity_id": str(opportunity.id),
                "package_fingerprint": result.fingerprint,
                "status": result.status,
                "included": len(result.included),
            },
        }],
    }


def _handle_document_generate(db: Session, context: dict[str, Any]) -> dict[str, Any]:
    """Render the documents a listing asks for, and file them as PENDING drafts.

    WHY THIS EXISTS. The pipeline ended at MATCHED: an agent could find an opportunity and prove the
    organisation eligible, and then stop, because nothing could produce the document the funder
    actually asks for. `DocumentVault.add_version()` has always required a `storage_key` - a file that
    already exists.

    WHAT IT DOES NOT DO. It does not approve, and it does not submit. Drafts go in PENDING and the
    vault enforces that, because an agent approving its own output into a submission is the exact
    failure the approval gate exists to prevent.

    IT REFUSES RATHER THAN INVENTING. A template placeholder with no verified fact behind it raises,
    and the workflow parks with the missing names in `waiting_on`. Putting a plausible-looking
    registration number in front of a donor in the organisation's name would be worse than producing
    nothing, and it is the one thing a generator like this must never do.

    IDEMPOTENT. Content is deterministic from facts and the listing, so a re-run produces the same
    checksum and the vault records the same version rather than a new one. A generator that emitted a
    timestamp would append a version on every attempt and leave a funder's "which draft did you send"
    unanswerable.
    """
    from agent import documents
    from agent.organisation_memory import DocumentVault, OrganisationMemory

    workflow = context["workflow"]
    agent = context["agent"]
    if workflow is None or agent is None:
        raise FleetError("document generation requires a workflow and an agent")

    opportunity = db.execute(
        select(models.Opportunity).where(models.Opportunity.id == workflow.subject_id)
    ).scalars().first()
    if opportunity is None:
        return {
            "summary": "the opportunity no longer exists",
            "summary_key": "documents.opportunity_missing",
            "next_state": models.AgentWorkflow.CANCELLED,
            "meaningful": False,
        }

    memory = OrganisationMemory(db, agent.org_id)
    vault = DocumentVault(db, agent.org_id)

    # `submission_facts()` returns only facts in a SUBMISSION-SAFE state. A document is a submission
    # artefact, so an unverified value must not reach a funder through one.
    facts = documents.facts_from_organisation(memory)

    # Which documents does THIS listing ask for? The same canonicaliser the readiness gate uses, so
    # the generator and the gate cannot disagree about what is required.
    from agent.document_types import required_types_for

    listing_text = " ".join(
        str(part or "")
        for part in (opportunity.title, opportunity.description, opportunity.eligibility_criteria)
    )
    wanted = required_types_for(listing_text)

    # Every application needs a cover letter, whether or not the listing spells it out.
    if "cover_letter" not in wanted:
        wanted.append("cover_letter")

    # The listing and the donor are not ORGANISATION facts; they come from the opportunity.
    facts = dict(facts)
    facts.setdefault("opportunity_title", opportunity.title or "")
    facts.setdefault("donor_name", opportunity.source_name or "")
    facts.setdefault("currency", opportunity.currency or "")
    facts.setdefault(
        "amount_requested", f"{opportunity.amount_max:,}" if opportunity.amount_max else ""
    )
    facts.setdefault("today", documents.today())

    store = documents.TemplateStore()
    generated: list[str] = []
    skipped: list[str] = []
    missing: dict[str, list[str]] = {}

    for doc_type in wanted:
        template = store.get(doc_type)
        if template is None:
            # Two different answers, and the difference matters to whoever reads the readiness report.
            # An EVIDENCE type is something the organisation must UPLOAD - no software can generate a
            # registration certificate. Anything else is simply a template we have not written yet.
            skipped.append(doc_type)
            continue
        try:
            document = documents.render(
                template, facts, filename_stem=str(opportunity.id)[:8]
            )
        except documents.MissingFact as exc:
            missing[doc_type] = [
                name for name in template.placeholders() if not str(facts.get(name, "") or "").strip()
            ]
            continue

        vault.add_version(
            title=document.title,
            doc_type=document.doc_type,
            storage_key=f"generated/{agent.org_id}/{opportunity.id}/{document.filename}",
            checksum_sha256=document.checksum_sha256,
            mime_type=document.mime_type,
            size_bytes=document.size_bytes,
            scope_ref=str(opportunity.id),
            uploaded_by=f"agent:{agent.id}",
        )
        generated.append(document.doc_type)

    if missing:
        # Park, naming exactly what the organisation must supply. The workflow engine gives this a
        # six-hour backoff, because the blocker is a person, not a transient fault.
        detail = "; ".join(f"{doc}: {', '.join(names)}" for doc, names in sorted(missing.items()))
        return {
            "summary": f"cannot generate {len(missing)} document(s) without: {detail}"[:1000],
            "summary_key": "documents.missing_facts",
            "next_state": models.AgentWorkflow.WAITING,
            "waiting_on": f"organisation information: {detail}"[:255],
            "meaningful": True,
            "activity_type": "document",
            "activity": "needs verified facts before documents can be prepared",
            "structured_data": {"generated": generated, "missing": missing, "skipped": skipped},
        }

    return {
        "summary": f"prepared {len(generated)} document(s) as drafts"
        + (f"; no template for {len(skipped)}" if skipped else ""),
        "summary_key": "documents.generated",
        "next_state": models.AgentWorkflow.COMPLETED,
        "meaningful": bool(generated),
        "activity_type": "document",
        "activity": f"prepared {len(generated)} application document(s) as drafts",
        "structured_data": {
            "generated": generated,
            "skipped": skipped,
            "must_be_uploaded": sorted(set(skipped) & set(documents.EVIDENCE_TYPES)),
        },
        # The package is assembled from what is now in the vault, so the next step is assembly.
        "enqueue": {
            "workflow_type": WORKFLOW_ASSEMBLE,
            "specialist_key": "COMPLIANCE",
            "correlation_id": context.get("correlation_id"),
        },
    }


def _handle_mail_process(db: Session, context: dict[str, Any]) -> dict[str, Any]:
    """Run the mail pipeline for a queued delivery. EARS ONLY — it cannot send.

    Email is one more wake condition for the **same shared fleet**, exactly like a
    new opportunity. There is no per-mailbox worker and no mail daemon: a worker
    picks up this job, loads the organisation's agent, and the database decides who
    the work belongs to.

    The handler is deliberately thin. All the reasoning lives in
    ``agent.mail.service``, and the capability ceiling is asserted inside it, so
    this cannot become a second, less careful path into the pipeline.

    The payload carries identifiers, not content. A job in Redis must never hold a
    mail body: the queue is the least protected and most replicated place in the
    system, and the brief forbids putting bodies there.
    """
    from agent.mail.service import GranadaMail

    workflow = context["workflow"]
    agent = context["agent"]

    # Read the wake payload from the WORKFLOW row, never from the job payload.
    # The workflow is organisation-scoped and RLS-protected; the queue is neither,
    # so identifiers taken from a message are read from the protected side. The job
    # payload is a hint for locating work and is treated as untrusted.
    payload = (workflow.context or {}).get("wake") or {}
    if not payload:
        # Fall back to the job payload only for a job enqueued by hand, which is an
        # administrative path rather than the normal one.
        payload = context["job"].payload or {}

    provider = payload.get("provider")
    provider_event_id = payload.get("provider_event_id")
    if not provider or not provider_event_id:
        return {
            "summary": "mail job carried no provider event to process",
            "summary_key": "mail.invalid_job",
            "next_state": models.AgentWorkflow.FAILED,
            "meaningful": False,
        }

    transport = _mail_transport(provider)
    if transport is None:
        # No credentials or no adapter: park rather than fail. The message is not
        # lost - the provider event row survives and a later sweep retries - and
        # marking it failed would hide a configuration gap as a processing error.
        return {
            "summary": f"no transport available for provider {provider}",
            "summary_key": "mail.transport_unavailable",
            "next_state": models.AgentWorkflow.WAITING,
            "waiting_on": f"mail transport for {provider} not configured",
            "meaningful": False,
        }

    from agent.mail.providers.base import ProviderEvent as MailProviderEventData

    mail = GranadaMail(db, org_id=agent.org_id, agent_id=agent.id, transport=transport)
    result = mail.ingest_webhook(
        provider=provider,
        event=MailProviderEventData(
            provider_event_id=provider_event_id,
            event_type=payload.get("event_type", "message.received"),
            provider_account_id=payload.get("provider_account_id"),
            provider_message_id=payload.get("provider_message_id"),
            provider_thread_id=payload.get("provider_thread_id"),
        ),
    )

    if result.error:
        return {
            "summary": f"mail processing failed: {result.error}"[:200],
            "summary_key": "mail.processing_failed",
            "activity_type": "mail",
            "structured_data": {"error": result.error[:200]},
            "next_state": models.AgentWorkflow.WAITING,
            "waiting_on": result.error[:255],
            "meaningful": False,
        }

    if result.duplicate_event or result.duplicate_message:
        return {
            "summary": "a duplicate delivery; no new work was created",
            "summary_key": "mail.duplicate",
            "activity_type": "mail",
            "structured_data": {
                "duplicate_event": result.duplicate_event,
                "duplicate_message": result.duplicate_message,
                "message_id": result.message_id,
            },
            "next_state": models.AgentWorkflow.COMPLETED,
            "meaningful": False,
        }

    structured = {
        "message_id": result.message_id,
        "thread_id": result.thread_id,
        "classification": result.classification,
        "correlation_state": result.correlation_state,
        "application_id": result.application_id,
        "deadline_id": result.deadline_id,
        "document_satisfied": result.document_satisfied,
        "draft_id": result.draft_id,
        "draft_status": result.draft_status,
        "security_flags": result.security_flags,
    }
    summary = (
        f"received a {result.classification or 'message'}"
        + (f", linked to an application ({result.correlation_state})" if result.application_id else
           f" ({result.correlation_state or 'unlinked'})")
        + (f", draft {result.draft_status}" if result.draft_status else "")
    )
    return {
        "summary": summary[:200],
        "summary_key": f"mail.{(result.draft_status or result.classification or 'received').lower()}",
        "activity_type": "mail",
        "structured_data": structured,
        "events": [{
            "event_type": "mail.workflow_completed",
            "stream": "granada:v1:mail:processed",
            "payload": structured,
        }],
        # STOP HERE. A draft exists and nothing has been sent. Phase 7a ends at
        # READY; outbound mail is a separate phase with its own ceiling.
        "next_state": models.AgentWorkflow.COMPLETED,
    }


#: Provider name -> adapter. Populated by the deployment: a provider with no
#: configured credentials is absent rather than broken, so the handler parks
#: instead of failing.
def _mail_transport(provider: str) -> Any:
    """Resolve a mail transport for a provider name.

    Delegates to the gateway's registry, which is the single place transports are
    selected. Returns ``None`` when nothing is configured — deliberately not an
    exception: an unconfigured provider is an operational state, not a processing
    error, and conflating the two would make a configuration gap look like mail
    corruption.
    """
    from agent.mail.gateway import get_transport

    return get_transport(provider)


def _outbound_transport(provider: str, *, db: Session = None, intent: Any = None) -> Any:
    """Resolve an OUTBOUND transport. Separate registry from inbound, on purpose.

    WHEN AN INTENT NAMES A MAILBOX, the transport is built from THAT mailbox's own credential.
    `get_outbound_transport(provider)` returns the deployment-wide transport registered under a provider
    name, so without this every organisation would send through whichever mailbox was configured - one
    organisation's reply arriving from another organisation's address, which looks deliberate to the
    recipient.

    The account is looked up rather than passed because the intent carries `mail_account_id`, and
    resolving it here keeps the caller's shape unchanged for the tests that pass a provider alone.
    """
    from agent.mail.gateway import get_outbound_transport, outbound_transport_for_account

    account_id = getattr(intent, "mail_account_id", None) if intent is not None else None
    if db is not None and account_id:
        account = db.execute(
            select(models.MailAccount).where(models.MailAccount.id == account_id)
        ).scalars().first()
        if account is not None:
            try:
                return outbound_transport_for_account(db, account=account, org_id=account.org_id)
            except Exception:  # noqa: BLE001 - a tenant mismatch must not become a crash here
                # A mismatch means the intent names a mailbox the intent's own organisation does not
                # own. Falling through to the provider-wide transport would be exactly the wrong
                # recovery, so this returns None and the caller parks the work.
                return None

    return get_outbound_transport(provider)


def _handle_mail_send(db: Session, context: dict[str, Any]) -> dict[str, Any]:
    """Execute a human-approved send intent.

    The handler's job is narrow: resolve the intent and provider, then hand both to
    ``SendService``, which owns the final authority check. **Nothing here decides
    whether to send.** No approval means no send, and that decision is made from
    durable state inside the service rather than from anything this handler saw.
    """
    from agent.mail.send_service import SendService

    workflow = context["workflow"]
    agent = context["agent"]

    payload = (workflow.context or {}).get("wake") or {}
    intent_id = payload.get("send_intent_id") or workflow.subject_id
    if not intent_id:
        return {
            "summary": "send workflow carried no intent",
            "summary_key": "mail.send_invalid_job",
            "next_state": models.AgentWorkflow.FAILED,
            "meaningful": False,
        }

    intent = db.execute(
        select(models.MailSendIntent).where(
            models.MailSendIntent.id == intent_id,
            models.MailSendIntent.org_id == agent.org_id,
        )
    ).scalars().first()
    if intent is None:
        return {
            "summary": "the send intent no longer exists",
            "summary_key": "mail.send_intent_missing",
            "next_state": models.AgentWorkflow.CANCELLED,
            "meaningful": False,
        }

    transport = _outbound_transport(intent.provider or payload.get("provider") or "", db=db, intent=intent)
    if transport is None:
        # No outbound provider configured is an operational state, not a failure.
        # Parking keeps the approved intent intact and resumable.
        return {
            "summary": f"no outbound provider configured for {intent.provider!r}",
            "summary_key": "mail.outbound_unavailable",
            "next_state": models.AgentWorkflow.WAITING,
            "waiting_on": f"outbound transport for {intent.provider} not configured",
            "meaningful": False,
        }

    service = SendService(
        db, org_id=agent.org_id, agent_id=agent.id, outbound=transport
    )
    result = service.execute_send(
        intent_id=intent.id, worker_id=context["job"].lease_owner or "worker"
    )

    structured = result.as_dict()
    if result.refused:
        # A refusal is a deliberate outcome with a name, not an error. Park it so a
        # person can act, and never retry it automatically: several refusal codes
        # mean "a human must decide", and a loop would hammer them.
        return {
            "summary": f"send refused: {result.refusal_code}"[:200],
            "summary_key": f"mail.send_refused.{result.refusal_code}".lower(),
            "activity_type": "mail",
            "structured_data": structured,
            "next_state": models.AgentWorkflow.WAITING,
            "waiting_on": (result.detail or result.refusal_code or "")[:255],
            "meaningful": False,
        }

    if result.outcome == "DELIVERY_UNKNOWN":
        # Reconcile work is created; a retry is forbidden until it reports back.
        AgentWake.schedule(
            db, agent=agent, workflow_type=WORKFLOW_MAIL_RECONCILE,
            specialist_key="EMAIL", subject_type="SEND_INTENT", subject_id=intent.id,
            payload_ref={"send_intent_id": intent.id, "provider": intent.provider},
        )
        return {
            "summary": "the provider's response was lost; reconciling before any retry",
            "summary_key": "mail.delivery_unknown",
            "activity_type": "mail",
            "structured_data": structured,
            "next_state": models.AgentWorkflow.COMPLETED,
        }

    return {
        "summary": (
            f"sent an approved reply to {', '.join(intent.to_addresses or [])}"[:200]
            if result.sent
            else f"send did not complete: {result.failure}"[:200]
        ),
        "summary_key": "mail.sent" if result.sent else "mail.send_failed",
        "activity_type": "mail",
        "structured_data": structured,
        "events": [{
            "event_type": "mail.send_completed",
            "stream": "granada:v1:mail:sent",
            "payload": structured,
        }],
        "next_state": models.AgentWorkflow.COMPLETED,
    }


def _handle_mail_reconcile(db: Session, context: dict[str, Any]) -> dict[str, Any]:
    """Establish what happened to an uncertain attempt, then stop.

    Reconciliation never sends. Its whole purpose is to convert "we do not know"
    into either "the provider has it" or "the provider provably does not", so that a
    later decision to retry rests on evidence rather than on a guess.
    """
    from agent.mail.send_service import SendService

    workflow = context["workflow"]
    agent = context["agent"]
    payload = (workflow.context or {}).get("wake") or {}
    intent_id = payload.get("send_intent_id") or workflow.subject_id

    intent = db.execute(
        select(models.MailSendIntent).where(
            models.MailSendIntent.id == intent_id,
            models.MailSendIntent.org_id == agent.org_id,
        )
    ).scalars().first()
    if intent is None:
        return {
            "summary": "the send intent no longer exists",
            "summary_key": "mail.reconcile_missing",
            "next_state": models.AgentWorkflow.CANCELLED,
            "meaningful": False,
        }

    transport = _outbound_transport(intent.provider or "", db=db, intent=intent)
    if transport is None:
        return {
            "summary": "no outbound provider available to reconcile against",
            "summary_key": "mail.outbound_unavailable",
            "next_state": models.AgentWorkflow.WAITING,
            "waiting_on": "outbound transport not configured",
            "meaningful": False,
        }

    service = SendService(db, org_id=agent.org_id, agent_id=agent.id, outbound=transport)
    result = service.reconcile(intent_id=intent.id)

    if result.refused and result.refusal_code == "STILL_UNKNOWN":
        # Retry the reconciliation later rather than concluding anything. Concluding
        # "not sent" from an absence of evidence is the mistake that duplicates mail.
        return {
            "summary": "reconciliation produced no decisive evidence; will look again",
            "summary_key": "mail.reconcile_inconclusive",
            "activity_type": "mail",
            "structured_data": result.as_dict(),
            "next_state": models.AgentWorkflow.WAITING,
            "waiting_on": "provider evidence inconclusive",
            "meaningful": False,
        }

    return {
        "summary": f"reconciliation: {result.outcome or result.refusal_code}"[:200],
        "summary_key": f"mail.reconciled.{(result.outcome or 'unknown').lower()}",
        "activity_type": "mail",
        "structured_data": result.as_dict(),
        "next_state": models.AgentWorkflow.COMPLETED,
    }


def _handle_mail_sync(db: Session, context: dict[str, Any]) -> dict[str, Any]:
    """Reconcile one mailbox: fetch anything a webhook never delivered.

    Bounded by the provider adapter. The durable cursor lives on the account, so a
    restart resumes rather than replaying the mailbox from the beginning.
    """
    from agent.mail.service import GranadaMail

    workflow = context["workflow"]
    agent = context["agent"]
    payload = (workflow.context or {}).get("wake") or {}
    account_id = payload.get("mail_account_id") or workflow.subject_id

    account = db.execute(
        select(models.MailAccount).where(
            models.MailAccount.id == account_id,
            models.MailAccount.org_id == agent.org_id,
        )
    ).scalars().first()
    if account is None:
        return {
            "summary": "the mail account no longer exists",
            "summary_key": "mail.sync_account_missing",
            "next_state": models.AgentWorkflow.CANCELLED,
            "meaningful": False,
        }

    transport = _mail_transport(account.provider)
    if transport is None:
        return {
            "summary": f"no transport configured for {account.provider}",
            "summary_key": "mail.transport_unavailable",
            "next_state": models.AgentWorkflow.WAITING,
            "waiting_on": f"mail transport for {account.provider} not configured",
            "meaningful": False,
        }

    mail = GranadaMail(db, org_id=agent.org_id, agent_id=agent.id, transport=transport)
    summary = mail.sync(account=account)

    # Rescheduling is the scheduler's job, not the handler's: a handler that
    # scheduled its own next run would be a per-mailbox timer by another name.
    return {
        "summary": f"reconciled {summary.get('messages_new', 0)} new message(s)"[:200],
        "summary_key": "mail.synced",
        "activity_type": "mail",
        "structured_data": summary,
        "next_state": models.AgentWorkflow.COMPLETED,
    }


def _settings() -> Any:
    try:
        from config import settings

        return settings
    except Exception:  # pragma: no cover - configuration unavailable
        return None

def _handle_browser_task(db: Session, context: dict[str, Any]) -> dict[str, Any]:
    """Run the browser worker for a package, as a leased capability. Off by default.

    SECTION 6. The worker is invoked through the existing job system - agent_workflows -> jobs ->
    job_attempts - rather than by a daemon or a process per organisation. One invocation, one
    browser, released in a finally block by the runtime.

    THE FLAG IS CHECKED FIRST AND THE HANDLER PARKS WHEN IT IS OFF. A registered workflow must not
    imply an enabled capability, and "the feature is switched off" is not a failure of the package -
    so the workflow WAITS rather than being marked complete or failed. That keeps the earlier
    runaway-loop defect fixed: a parking state must not be rescheduled with no new information.

    A MISSING MODEL IS NOT A MISSING DOCUMENT. If vision is required and no multimodal model is
    configured, that is a routing refusal (NoCapableModel) and the workflow parks. It is never
    reported as the organisation failing to supply something.
    """
    from agent import browser_invocation
    from agent.browser_boundary import build_task

    workflow = context["workflow"]
    agent = context["agent"]
    if workflow is None or agent is None:
        raise FleetError("browser execution requires a workflow and an agent")

    # SETTINGS COME FROM THE CONTEXT, NOT FROM A DB ACCESSOR I ASSUMED.
    #
    # The first version of this handler imported `agent.settings_store.get_settings`, which does not
    # exist - I wrote the call before checking, the same mistake this project keeps recording. There
    # is no Setting model in models.py and admin.py is not at the path I assumed either, so rather
    # than guess at a mechanism, the caller supplies the mapping. That is honest about where the
    # values come from, and it makes the handler testable with a plain dict.
    #
    # Defaults to EMPTY, which means the flag reads as off - so an unwired caller cannot accidentally
    # enable browser execution.
    settings = dict(context.get("settings") or {})
    if not browser_invocation.enabled(settings):
        return {
            "summary": (
                "browser execution is disabled; the package is prepared and waiting, and nothing was "
                "opened"
            ),
            "summary_key": "browser.disabled",
            "next_state": models.AgentWorkflow.WAITING,
            "meaningful": False,
        }

    # THE APPLICATION IS FOUND BY (org, opportunity), NOT BY workflow_id.
    #
    # `models.Application` has no `workflow_id` column - it carries org_id and opportunity_id, and the
    # workflow carries the opportunity as its subject. The regression guard
    # (test_no_module_references_a_missing_model_attribute) caught the invented column, which is
    # exactly what it is for: a nonexistent attribute would be an AttributeError at runtime on a live
    # workflow, not at import.
    application = db.execute(
        select(models.Application).where(
            models.Application.org_id == agent.org_id,
            models.Application.opportunity_id == workflow.subject_id,
        )
    ).scalars().first()
    if application is None:
        return {
            "summary": "no application is attached to this workflow",
            "summary_key": "browser.no_application",
            "next_state": models.AgentWorkflow.WAITING,
            "meaningful": False,
        }

    package = db.execute(
        select(models.SubmissionPackage).where(
            models.SubmissionPackage.application_id == application.id
        )
    ).scalars().first()
    if package is None:
        # Nothing to open. PARK rather than fail: the package is assembled by another step, and
        # treating its absence as an error would make an ordering problem look like a crash.
        return {
            "summary": "no assembled package yet for this application",
            "summary_key": "browser.no_package",
            "next_state": models.AgentWorkflow.WAITING,
            "meaningful": False,
        }

    try:
        task = build_task(
            db,
            package,
            action_scope=_browser_scope_for(package, settings),
        )
    except Exception as exc:
        # build_task refuses an unready package. That refusal is the readiness engine doing its job,
        # and it is a WAIT - the package is not wrong, it is not finished.
        return {
            "summary": f"the package is not ready to be opened: {exc}",
            "summary_key": "browser.package_not_ready",
            "next_state": models.AgentWorkflow.WAITING,
            "meaningful": False,
        }

    invoker = browser_invocation.SubprocessInvoker(settings.get(browser_invocation.BROWSER_WORKER_COMMAND, ""))
    outcome = browser_invocation.invoke(
        task,
        invoker=invoker,
        settings=settings,
        submission_authorised=False,   # never granted here; submission_authority owns that decision
    )

    return {
        "summary": _browser_summary(outcome),
        "summary_key": f"browser.{outcome.status.lower()}",
        # COMPLETED only when the run actually completed. BLOCKED, UNCERTAIN and UNAVAILABLE all
        # park, because none of them is a finished task and rescheduling them with no new
        # information is the runaway loop this codebase already had to fix once.
        "next_state": (
            models.AgentWorkflow.COMPLETED
            if outcome.status == "COMPLETED"
            else models.AgentWorkflow.WAITING
        ),
        "meaningful": outcome.status == "COMPLETED",
        "outcome": outcome.to_dict(),
    }


def _browser_summary(outcome: Any) -> str:
    """Operator-facing text. Names what happened, never reports a submission that did not occur."""
    if outcome.status == "COMPLETED":
        return (
            f"the browser completed {len(outcome.completed_steps)} step(s) and attached "
            f"{len(outcome.uploaded)} document(s); no submission was made"
        )
    if outcome.status == "BLOCKED":
        detail = next((p.get("detail", "") for p in outcome.problems), "the page could not be completed")
        return f"the browser stopped and needs attention: {detail}"
    if outcome.status == "UNCERTAIN":
        return (
            "the browser could not confirm the outcome; this must be reconciled rather than retried"
        )
    if outcome.status == "DISABLED":
        return "browser execution is disabled"
    if outcome.status == "UNAVAILABLE":
        return "the browser worker is not available on this host"
    return f"the browser reported {outcome.status}"


def _browser_scope_for(package: Any, settings: dict[str, Any]) -> Any:
    """The host allowlist for one package, from configuration.

    EXACT HOSTS ONLY. browser_boundary.validate_task refuses a wildcard, because suffix matching
    lets `notfunder.example` match `funder.example`, and this builds the value it checks.
    """
    from agent.browser_boundary import ActionScope

    raw = settings.get("browser_allowed_hosts") or []
    hosts = tuple(str(h).strip() for h in raw if str(h).strip())
    return ActionScope(
        portal_name=str(settings.get("browser_portal_name") or "unknown"),
        allowed_hosts=hosts,
    )