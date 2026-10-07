"""Scoped concurrency control for agent work.

What must not happen, and what must
-----------------------------------
The brief is explicit that the Granada Agent must **not** be locked globally. An
agent working on Application 1's research and Application 2's matching at the same
time is correct and desirable; serialising everything behind one agent-level lock
would make a busy organisation's agent slower than a queue.

What must be prevented is *incompatible duplicate work* on the same subject:

    same agent + same opportunity + same work type  -> one execution
    same agent + same opportunity + same research version -> one record

**The protection is durable, not advisory.** Every guard here is a database
uniqueness constraint or a row lock, because a Redis lock dies with the connection
that took it and cannot be the only defence. Redis may supplement; it cannot be
the mechanism.

Three layers, each doing one job:

``agent_workflows.uq_workflow_agent_subject``
    Unique on (agent_id, workflow_type, subject_type, subject_id). Two workflows
    for the same subject are the same work; the second creation wakes the first.
``jobs`` idempotency key, unique on (org_id, job_type, idempotency_key)
    ``{workflow_id}:{attempt}``, so a retried dispatch of the same attempt cannot
    produce a second job.
``donor_research.uq_research_opportunity_version``
    Unique on (opportunity_id, agent_id, version), and ``research()`` is idempotent
    per opportunity revision, so a recovered job cannot append a duplicate.

Plus ``JobLedger.claim``'s ``SELECT ... FOR UPDATE SKIP LOCKED``, which is what
makes two workers unable to hold the same job.

Lock keys are deliberately narrow: the subject is the *unit of work*, not the
agent. Adding ``application_id`` to the key is what allows two applications of one
agent to progress concurrently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)


class ConcurrencyError(RuntimeError):
    """Base class for concurrency refusals."""


class AlreadyRunning(ConcurrencyError):
    """An incompatible unit of work is already in flight for this subject."""


@dataclass(frozen=True)
class WorkKey:
    """The narrowest key that makes two units of work incompatible.

    ``subject_id`` is the opportunity or application the work concerns, **not**
    the agent. That is the whole design: one agent may run many subjects at once.
    """

    agent_id: str
    org_id: str
    work_type: str
    subject_id: Optional[str]
    version: Optional[int] = None

    @property
    def lock_name(self) -> str:
        parts = [self.agent_id, self.work_type, self.subject_id or "none"]
        if self.version is not None:
            parts.append(str(self.version))
        return "granada:v1:lock:" + ":".join(parts)


class ConcurrencyGuard:
    """Durable, scoped guards over agent work."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    def in_flight(
        self, *, agent_id: str, work_type: str, subject_id: Optional[str]
    ) -> Optional[models.Job]:
        """Whether an incompatible unit of work is already running for this subject.

        Scoped to the **subject**, not the agent: a different opportunity is
        different work and must not be blocked by this one.
        """
        stmt = select(models.Job).where(
            models.Job.agent_id == agent_id,
            models.Job.job_type == work_type,
            models.Job.state == models.Job.RUNNING,
        )
        if subject_id is not None:
            # The job row does not carry the subject; the workflow does, and the
            # job points at the workflow. Joining is what keeps the key narrow
            # instead of denormalising the subject onto every job.
            stmt = stmt.join(
                models.AgentWorkflow, models.AgentWorkflow.id == models.Job.workflow_id
            ).where(models.AgentWorkflow.subject_id == subject_id)
        return self.db.execute(stmt).scalars().first()

    def assert_claimable(
        self, *, agent_id: str, work_type: str, subject_id: Optional[str]
    ) -> None:
        """Refuse if the same subject's work type is already in flight.

        Called by the worker *before* leasing. The lease itself is atomic, so this
        is a second, narrower guard rather than the primary one - but it is the one
        that produces a readable reason instead of a silent skip.
        """
        running = self.in_flight(agent_id=agent_id, work_type=work_type, subject_id=subject_id)
        if running is not None:
            raise AlreadyRunning(
                f"{work_type} is already running for subject {subject_id} "
                f"(job {running.id}, worker {running.lease_owner})"
            )

    # ------------------------------------------------------------------
    def research_version_exists(
        self, *, agent_id: str, opportunity_id: str, opportunity_version: int
    ) -> bool:
        """Whether this opportunity revision has already been researched.

        The guard that makes a recovered research job safe: re-running it returns
        the existing record rather than appending a second version for one
        revision.
        """
        row = self.db.execute(
            select(models.DonorResearch.id).where(
                models.DonorResearch.agent_id == agent_id,
                models.DonorResearch.opportunity_id == opportunity_id,
                models.DonorResearch.opportunity_version == opportunity_version,
            )
        ).first()
        return row is not None

    def assert_advanceable(self, workflow: models.AgentWorkflow) -> None:
        """Refuse to advance a workflow that another worker is mid-step on.

        A workflow in RUNNING whose lease has not expired belongs to somebody. Its
        lease expiring is the *recovery* path, not a licence to run it twice.
        """
        if workflow.state != models.AgentWorkflow.RUNNING:
            return
        if workflow.next_run_at is None:
            return
        moment = datetime.now(timezone.utc)
        next_run = workflow.next_run_at
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
        if next_run > moment:
            raise AlreadyRunning(
                f"workflow {workflow.id} is not due until {next_run.isoformat()}"
            )

    # ------------------------------------------------------------------
    def parallel_capacity(self, agent_id: str) -> dict[str, Any]:
        """What this agent has in flight, per subject.

        Exposed so the design claim is checkable rather than asserted: one agent
        holding several *different* subjects at once is correct, and this is how a
        test proves the locking is not agent-wide.
        """
        rows = self.db.execute(
            select(models.AgentWorkflow.subject_id, models.AgentWorkflow.workflow_type)
            .where(
                models.AgentWorkflow.agent_id == agent_id,
                models.AgentWorkflow.state == models.AgentWorkflow.RUNNING,
            )
        ).all()
        return {
            "in_flight": len(rows),
            "subjects": sorted({r[0] for r in rows if r[0]}),
            "work_types": sorted({r[1] for r in rows}),
        }

    def stale_leases(self, *, grace_seconds: int = 0) -> list[models.Job]:
        """Jobs whose lease has lapsed - the recovery work list.

        Read-only. The mutation belongs to ``JobLedger.reclaim_expired``, so there
        is one code path that changes a lease and not two.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)
        return list(
            self.db.execute(
                select(models.Job).where(
                    models.Job.state == models.Job.RUNNING,
                    models.Job.lease_expires_at.isnot(None),
                    models.Job.lease_expires_at < cutoff,
                )
            ).scalars()
        )
