"""Administrative recovery commands.

The brief requires that normal recovery does not mean editing SQL. These are
supported operations with the properties an operational tool must have:

**They use the application services**, not raw ``UPDATE`` statements. That matters
more than it sounds: a raw update bypasses the state machine, the authority
checks and the activity ledger, so the database would end up in a state the
services would never have produced and nobody could explain how.

**Every action is audited.** Each command writes to ``audit_logs`` *and* to
``agent_activity`` as an INTERNAL entry, so "why did this workflow move" is
answerable after the fact. An administrative action that leaves no trace is
indistinguishable from an intrusion.

**They refuse illegal operations.** Retrying a job whose specialist is disabled,
resuming a cancelled workflow, or cancelling a terminal one all fail with a
reason rather than doing something surprising.

**They respect tenancy.** Every command is constructed for one organisation and
resolves rows through it, so an operator's typo cannot cross tenants.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from agent.granada_agent import AgentError, GranadaAgentService
from agent.mail.approval import ApprovalService
from agent.specialists import SpecialistError, resolve
from agent.workflow_engine import FleetDispatcher
from events.ledger import JobLedger
from observability import metrics

logger = logging.getLogger(__name__)


class AdminError(RuntimeError):
    """Base class for command failures."""


class NotFound(AdminError):
    """The row does not exist for this organisation."""


class Refused(AdminError):
    """The operation is legal in general but not for this row."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CommandResult:
    """What a command did, in a shape a CLI can print and a test can assert."""

    command: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    refused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "ok": self.ok,
            "refused": self.refused,
            "detail": self.detail,
            "data": self.data,
        }


class AdminCommands:
    """Operational commands, scoped to one organisation.

    ``actor_id`` is required for every mutating command. An unattributed
    administrative action cannot be investigated, and "the system did it" is not
    an answer when the system is what is being audited.
    """

    def __init__(self, db: Session, org_id: str, *, actor_id: Optional[str] = None) -> None:
        if not org_id:
            raise AdminError("org_id is required; an unscoped admin command is refused")
        self.db = db
        self.org_id = org_id
        self.actor_id = actor_id

    # ------------------------------------------------------------------
    def _require_actor(self, command: str) -> None:
        if not self.actor_id:
            raise Refused(
                f"{command} requires an attributed operator; an unattributed "
                "administrative action cannot be investigated afterwards"
            )

    def _audit(self, *, action: str, subject: Optional[str], detail: str) -> None:
        """Write the operator-visible trail.

        Two records on purpose. ``audit_logs`` is the security trail (who did
        what, from where); ``agent_activity`` with INTERNAL visibility is the
        operational trail the agent's own dashboard can show without exposing it
        to the customer.
        """
        self.db.add(
            models.AuditLog(
                org_id=self.org_id,
                user_id=self.actor_id,
                actor_user_id=self.actor_id,
                event=f"admin.{action}"[:50],
                # The real column name, verified against the model rather than
                # assumed: AuditLog uses payload_json, and a wrong keyword here
                # would raise at flush time inside a recovery command.
                payload_json={"subject": subject, "detail": detail},
                created_at=_now(),
            )
        )
        agent = GranadaAgentService(self.db, self.org_id).get()
        if agent is not None:
            self.db.add(
                models.AgentActivity(
                    agent_id=agent.id,
                    org_id=self.org_id,
                    activity_type="admin",
                    summary_key=f"admin.{action}",
                    subject_type="ADMIN",
                    subject_id=subject,
                    structured_data={"detail": detail, "actor_id": self.actor_id},
                    visibility=models.AgentActivity.VISIBILITY_INTERNAL,
                    occurred_at=_now(),
                )
            )
        self.db.flush()
        metrics.inc("admin.command", command=action)

    # ------------------------------------------------------------------
    # Read-only
    # ------------------------------------------------------------------
    def show_agent(self, agent_id: Optional[str] = None) -> CommandResult:
        service = GranadaAgentService(self.db, self.org_id)
        agent = service.get()
        if agent is None:
            return CommandResult("show-agent", False, "no agent for this organisation")
        if agent_id is not None and agent.id != agent_id:
            return CommandResult("show-agent", False, "agent belongs to another organisation")
        return CommandResult(
            "show-agent", True, agent.display_name,
            {"agent": {
                "id": agent.id, "status": agent.status, "autonomy": agent.autonomy,
                "version": agent.version,
                "last_active_at": agent.last_active_at.isoformat() if agent.last_active_at else None,
                "specialists": [
                    {"key": s.key, "status": s.status, "activity": s.current_activity,
                     "runs": s.runs_completed}
                    for s in service.specialists()
                ],
                "status": service.status().as_dict(),
            }},
        )

    def show_workflow(self, workflow_id: str) -> CommandResult:
        workflow = self._workflow(workflow_id)
        jobs = self.db.execute(
            select(models.Job).where(models.Job.workflow_id == workflow.id)
        ).scalars().all()
        return CommandResult(
            "show-workflow", True, f"{workflow.workflow_type} is {workflow.state}",
            {"workflow": {
                "id": workflow.id, "type": workflow.workflow_type,
                "state": workflow.state, "specialist": workflow.specialist_key,
                "waiting_on": workflow.waiting_on, "attempts": workflow.attempts,
                "next_run_at": workflow.next_run_at.isoformat() if workflow.next_run_at else None,
                "subject": {"type": workflow.subject_type, "id": workflow.subject_id},
                "jobs": [
                    {"id": j.id, "state": j.state, "attempt": j.attempt,
                     "lease_owner": j.lease_owner, "last_error": j.last_error}
                    for j in jobs
                ],
            }},
        )

    def show_job(self, job_id: str) -> CommandResult:
        job = self._job(job_id)
        attempts = JobLedger(self.db).attempts(job.id)
        return CommandResult(
            "show-job", True, f"{job.job_type} is {job.state}",
            {"job": {
                "id": job.id, "type": job.job_type, "state": job.state,
                "attempt": job.attempt, "max_attempts": job.max_attempts,
                "agent_id": job.agent_id, "agent_version": job.agent_version,
                "workflow_id": job.workflow_id, "lease_owner": job.lease_owner,
                "available_at": job.available_at.isoformat() if job.available_at else None,
                "failure_category": job.failure_category, "last_error": job.last_error,
                "attempts": [
                    {"attempt": a.attempt, "outcome": a.outcome, "error": a.error,
                     "duration_ms": a.duration_ms}
                    for a in attempts
                ],
            }},
        )

    def list_stuck_workflows(self, *, older_than_minutes: int = 30) -> CommandResult:
        """Workflows that are due, unhandled, or running with a lapsed lease.

        The definition is deliberately broad: anything an operator would need to
        look at. A narrow definition would leave real problems outside the report
        and give false confidence.
        """
        moment = _now()
        stale_before = moment - timedelta(minutes=older_than_minutes)
        rows = self.db.execute(
            select(models.AgentWorkflow).where(
                models.AgentWorkflow.org_id == self.org_id,
                models.AgentWorkflow.state.in_(
                    [models.AgentWorkflow.PENDING, models.AgentWorkflow.RUNNING,
                     models.AgentWorkflow.BLOCKED]
                ),
            ).order_by(models.AgentWorkflow.updated_at.asc().nullsfirst())
        ).scalars().all()

        stuck = []
        for workflow in rows:
            updated = workflow.updated_at or workflow.created_at
            if updated is not None and updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            overdue = workflow.next_run_at is not None and (
                (workflow.next_run_at.replace(tzinfo=timezone.utc)
                 if workflow.next_run_at.tzinfo is None else workflow.next_run_at) < moment
            )
            if overdue or (updated is not None and updated < stale_before):
                stuck.append({
                    "id": workflow.id, "type": workflow.workflow_type,
                    "state": workflow.state, "waiting_on": workflow.waiting_on,
                    "attempts": workflow.attempts,
                    "updated_at": updated.isoformat() if updated else None,
                    "reason": "overdue" if overdue else "no progress",
                })
        return CommandResult(
            "list-stuck-workflows", True, f"{len(stuck)} workflow(s) need attention",
            {"stuck": stuck},
        )

    # ------------------------------------------------------------------
    # Mutating
    # ------------------------------------------------------------------
    def retry_step(self, workflow_id: str) -> CommandResult:
        """Put a workflow's failed step back on the queue.

        Refuses when the specialist is not executable, because a retry that can
        only fail again is not a recovery - it is a loop.
        """
        self._require_actor("retry-step")
        workflow = self._workflow(workflow_id)

        if workflow.state in (models.AgentWorkflow.COMPLETED, models.AgentWorkflow.CANCELLED):
            raise Refused(
                f"workflow {workflow.id} is {workflow.state}; a finished workflow is "
                "not retried, it is re-created by re-scheduling the subject"
            )
        if workflow.specialist_key is not None:
            spec = resolve(workflow.specialist_key)
            if workflow.workflow_type not in spec.handlers:
                raise Refused(
                    f"{spec.display_name} has no handler for {workflow.workflow_type!r}; "
                    "retrying would fail again"
                )

        workflow.state = models.AgentWorkflow.PENDING
        workflow.next_run_at = _now()
        workflow.waiting_on = None
        workflow.updated_at = _now()
        self.db.flush()

        self._audit(action="retry_step", subject=workflow.id,
                    detail=f"requeued {workflow.workflow_type}")
        return CommandResult("retry-step", True, "workflow requeued",
                             {"workflow_id": workflow.id})

    def requeue_job(self, job_id: str) -> CommandResult:
        """Requeue a failed or dead-lettered job.

        Only where safe: a job that succeeded is not requeued, because re-running
        completed work is how duplicates are made.
        """
        self._require_actor("requeue-job")
        job = self._job(job_id)

        if job.state == models.Job.SUCCEEDED:
            raise Refused(
                f"job {job.id} already succeeded; re-running completed work is how "
                "duplicates are created"
            )
        if job.state == models.Job.RUNNING:
            raise Refused(
                f"job {job.id} is RUNNING and leased by {job.lease_owner}; reclaim or "
                "wait for the lease rather than requeueing underneath a live worker"
            )
        if job.agent_id is None and job.job_type not in {"system_task"}:
            raise Refused(f"job {job.id} names no agent and is not a system task")

        previous = job.state
        job.state = models.Job.QUEUED
        job.available_at = _now()
        job.lease_owner = None
        job.lease_expires_at = None
        job.failure_category = None
        job.updated_at = _now()
        self.db.flush()

        self._audit(action="requeue_job", subject=job.id,
                    detail=f"{previous} -> QUEUED")
        return CommandResult("requeue-job", True, f"job requeued from {previous}",
                             {"job_id": job.id, "previous_state": previous})

    def resume_workflow(self, workflow_id: str) -> CommandResult:
        """Clear a wait and make a workflow due."""
        self._require_actor("resume-workflow")
        workflow = self._workflow(workflow_id)

        if workflow.state in (models.AgentWorkflow.COMPLETED, models.AgentWorkflow.CANCELLED):
            raise Refused(f"workflow {workflow.id} is {workflow.state} and cannot be resumed")
        agent = GranadaAgentService(self.db, self.org_id).get()
        if agent is not None and agent.status != models.GranadaAgent.ACTIVE:
            raise Refused(
                f"the agent is {agent.status}; resuming a workflow of a paused agent "
                "would be undone at the next authority checkpoint"
            )

        workflow.state = models.AgentWorkflow.PENDING
        workflow.waiting_on = None
        workflow.next_run_at = _now()
        workflow.updated_at = _now()
        self.db.flush()
        self._audit(action="resume_workflow", subject=workflow.id, detail="wait cleared")
        return CommandResult("resume-workflow", True, "workflow resumed",
                             {"workflow_id": workflow.id})

    def cancel_workflow(self, workflow_id: str, *, reason: str = "") -> CommandResult:
        """Cancel a workflow. Terminal states refuse, because cancelling an award
        or a rejection would rewrite an outcome."""
        self._require_actor("cancel-workflow")
        workflow = self._workflow(workflow_id)

        if workflow.state in (models.AgentWorkflow.COMPLETED, models.AgentWorkflow.CANCELLED):
            raise Refused(f"workflow {workflow.id} is already {workflow.state}")

        workflow.state = models.AgentWorkflow.CANCELLED
        workflow.waiting_on = reason[:255] or "cancelled by an operator"
        workflow.updated_at = _now()
        self.db.flush()
        self._audit(action="cancel_workflow", subject=workflow.id,
                    detail=reason or "cancelled by an operator")
        return CommandResult("cancel-workflow", True, "workflow cancelled",
                             {"workflow_id": workflow.id})

    def pause_agent(self, *, reason: str = "") -> CommandResult:
        self._require_actor("pause-agent")
        service = GranadaAgentService(self.db, self.org_id)
        agent = service.get()
        if agent is None:
            raise NotFound("no agent for this organisation")
        if agent.status == models.GranadaAgent.PAUSED:
            return CommandResult("pause-agent", True, "already paused",
                                 {"agent_id": agent.id})
        previous = agent.status
        agent.status = models.GranadaAgent.PAUSED
        agent.version += 1
        agent.updated_at = _now()
        self.db.flush()
        self._audit(action="pause_agent", subject=agent.id,
                    detail=reason or f"{previous} -> PAUSED")
        return CommandResult("pause-agent", True, "agent paused",
                             {"agent_id": agent.id, "previous_status": previous})

    def resume_agent(self) -> CommandResult:
        self._require_actor("resume-agent")
        service = GranadaAgentService(self.db, self.org_id)
        agent = service.get()
        if agent is None:
            raise NotFound("no agent for this organisation")
        if agent.status == models.GranadaAgent.ACTIVE:
            return CommandResult("resume-agent", True, "already active",
                                 {"agent_id": agent.id})
        previous = agent.status
        agent.status = models.GranadaAgent.ACTIVE
        agent.version += 1
        agent.updated_at = _now()
        self.db.flush()
        self._audit(action="resume_agent", subject=agent.id, detail=f"{previous} -> ACTIVE")
        return CommandResult("resume-agent", True, "agent resumed",
                             {"agent_id": agent.id, "previous_status": previous})

    def show_send_intent(self, intent_id: str) -> CommandResult:
        """Everything about one outbound message, including its approvals."""
        intent = self._send_intent(intent_id)
        approvals = self.db.execute(
            select(models.MailApproval).where(
                models.MailApproval.send_intent_id == intent.id
            ).order_by(models.MailApproval.approved_at.asc())
        ).scalars().all()
        attempts = self.db.execute(
            select(models.MailSendAttempt).where(
                models.MailSendAttempt.send_intent_id == intent.id
            ).order_by(models.MailSendAttempt.attempt_number.asc())
        ).scalars().all()

        live = ApprovalService(self.db, org_id=self.org_id).active_approval(intent)
        return CommandResult(
            "show-send-intent", True, f"{intent.status} ({intent.risk_class})",
            {"send_intent": {
                "id": intent.id,
                "status": intent.status,
                "status_reason": intent.status_reason,
                "risk_class": intent.risk_class,
                "fingerprint": intent.message_fingerprint,
                "from": intent.from_address,
                "to": intent.to_addresses,
                "subject": intent.subject,
                "application_id": intent.application_id,
                "thread_id": intent.thread_id,
                "granada_message_ref": intent.granada_message_ref,
                "provider_submission_id": intent.provider_submission_id,
                "delivery_state": intent.delivery_state,
                "attempt_count": intent.attempt_count,
                "sent_at": intent.sent_at.isoformat() if intent.sent_at else None,
                "approval_currently_authorises_this_message": live is not None,
                "approvals": [
                    {"decision": a.decision, "by": a.approved_by,
                     "at": a.approved_at.isoformat() if a.approved_at else None,
                     "fingerprint": a.fingerprint, "status": a.status,
                     "matches_current": a.fingerprint == intent.message_fingerprint}
                    for a in approvals
                ],
                "attempts": [
                    {"number": a.attempt_number, "result": a.result,
                     "error_code": a.error_code, "provider_submission_id": a.provider_submission_id,
                     "reconciliation_state": a.reconciliation_state,
                     "started_at": a.started_at.isoformat() if a.started_at else None}
                    for a in attempts
                ],
            }},
        )

    def show_send_attempts(self, intent_id: str) -> CommandResult:
        """The append-only attempt history: what Granada told each provider, and when."""
        intent = self._send_intent(intent_id)
        attempts = self.db.execute(
            select(models.MailSendAttempt).where(
                models.MailSendAttempt.send_intent_id == intent.id
            ).order_by(models.MailSendAttempt.attempt_number.asc())
        ).scalars().all()
        return CommandResult(
            "show-send-attempts", True, f"{len(attempts)} attempt(s)",
            {"send_intent_id": intent.id, "attempts": [
                {"number": a.attempt_number, "attempt_id": a.attempt_id,
                 "provider": a.provider, "result": a.result,
                 "error_code": a.error_code, "safe_error_summary": a.safe_error_summary,
                 "provider_submission_id": a.provider_submission_id,
                 "reconciliation_state": a.reconciliation_state,
                 "started_at": a.started_at.isoformat() if a.started_at else None,
                 "finished_at": a.finished_at.isoformat() if a.finished_at else None}
                for a in attempts
            ]},
        )

    def cancel_send(self, intent_id: str, reason: str = "") -> CommandResult:
        """Stop an outbound message before it leaves.

        Refuses once the message is SENDING, because a send in flight cannot be
        called back and pretending otherwise would leave a cancelled-looking record
        for a message that reaches a funder.
        """
        self._require_actor("cancel-send")
        intent = self._send_intent(intent_id)
        if intent.status in models.MailSendIntent.TERMINAL:
            raise Refused(f"send intent {intent.id} is already {intent.status}")
        if intent.status == models.MailSendIntent.SENDING:
            raise Refused(
                f"send intent {intent.id} is SENDING; a message already handed to the "
                "provider cannot be recalled. Use reconcile-send once the outcome is known."
            )

        previous = intent.status
        intent.status = models.MailSendIntent.CANCELLED
        intent.status_reason = reason or "cancelled by an operator"
        # Revoke any live approval too, so a later retry cannot pick it up.
        for approval in self.db.execute(
            select(models.MailApproval).where(
                models.MailApproval.send_intent_id == intent.id,
                models.MailApproval.status == models.MailApproval.STATUS_ACTIVE,
            )
        ).scalars():
            approval.status = models.MailApproval.STATUS_REVOKED
            approval.revoked_at = _now()
            approval.revoked_by = self.actor_id
        self.db.flush()
        self._audit(action="cancel_send", subject=intent.id, detail=f"{previous} -> CANCELLED")
        return CommandResult("cancel-send", True, f"cancelled from {previous}",
                             {"send_intent_id": intent.id, "previous_status": previous})

    def reconcile_send(self, intent_id: str) -> CommandResult:
        """Ask the provider what happened to an uncertain attempt.

        This is the **only** sanctioned way out of DELIVERY_UNKNOWN, and it never
        sends. It establishes evidence; whether to retry is then a separate decision
        made on that evidence.
        """
        self._require_actor("reconcile-send")
        from agent.mail.send_service import SendService
        from agent.mail.gateway import get_outbound_transport

        intent = self._send_intent(intent_id)
        transport = get_outbound_transport(intent.provider or "")
        if transport is None:
            raise Refused(
                f"no outbound provider is configured for {intent.provider!r}; nothing to "
                "reconcile against"
            )
        service = SendService(
            self.db, org_id=self.org_id, agent_id=intent.agent_id, outbound=transport
        )
        result = service.reconcile(intent_id=intent.id)
        self._audit(action="reconcile_send", subject=intent.id,
                    detail=f"{result.outcome or result.refusal_code}")
        return CommandResult(
            "reconcile-send", not result.refused,
            f"{result.outcome or result.refusal_code}",
            result.as_dict(),
        )

    def retry_confirmed_not_sent(self, intent_id: str) -> CommandResult:
        """Requeue a send that the provider **confirmed** it did not accept.

        Refuses anything else. A `DELIVERY_UNKNOWN` intent must be reconciled first,
        because retrying an unknown outcome can send the same message twice - which is
        precisely the harm this whole phase is built to prevent.
        """
        self._require_actor("retry-confirmed-not-sent")
        intent = self._send_intent(intent_id)

        if intent.status == models.MailSendIntent.DELIVERY_UNKNOWN:
            raise Refused(
                "the outcome of the last attempt is UNKNOWN. Reconcile first: a retry "
                "now may send this message to the funder twice."
            )
        if intent.status == models.MailSendIntent.SENT:
            raise Refused("this message was already accepted by the provider")

        attempt = self.db.execute(
            select(models.MailSendAttempt).where(
                models.MailSendAttempt.send_intent_id == intent.id
            ).order_by(models.MailSendAttempt.attempt_number.desc())
        ).scalars().first()
        if attempt is None or attempt.result != models.MailSendAttempt.CONFIRMED_NOT_SENT:
            raise Refused(
                "no attempt has been confirmed as not-accepted for this intent; a retry "
                "requires positive evidence that nothing was delivered"
            )

        previous = intent.status
        intent.status = models.MailSendIntent.QUEUED
        intent.retry_not_before = _now()
        intent.status_reason = "requeued after a confirmed non-acceptance"
        self.db.flush()
        self._audit(action="retry_confirmed_not_sent", subject=intent.id,
                    detail=f"{previous} -> QUEUED (attempt {attempt.attempt_number} confirmed not sent)")
        return CommandResult("retry-confirmed-not-sent", True, "requeued",
                             {"send_intent_id": intent.id, "previous_status": previous})

    def drain_fleet(self, *, rounds: int = 3) -> CommandResult:
        """Run bounded sweeps. Convenience for an operator after a fix."""
        self._require_actor("drain-fleet")
        dispatcher = FleetDispatcher(self.db)
        totals = {"dispatched": 0, "duplicates": 0, "scanned": 0}
        for _ in range(rounds):
            result = dispatcher.dispatch_once()
            totals["dispatched"] += result.dispatched
            totals["duplicates"] += result.duplicates
            totals["scanned"] += result.scanned
        self.db.flush()
        self._audit(action="drain_fleet", subject=None, detail=str(totals))
        return CommandResult("drain-fleet", True, "sweeps completed", totals)

    # ------------------------------------------------------------------
    def _workflow(self, workflow_id: str) -> models.AgentWorkflow:
        workflow = self.db.execute(
            select(models.AgentWorkflow).where(
                models.AgentWorkflow.id == workflow_id,
                models.AgentWorkflow.org_id == self.org_id,
            )
        ).scalars().first()
        if workflow is None:
            raise NotFound(
                f"no workflow {workflow_id} in this organisation"
            )
        return workflow

    def _send_intent(self, intent_id: str) -> models.MailSendIntent:
        intent = self.db.execute(
            select(models.MailSendIntent).where(
                models.MailSendIntent.id == intent_id,
                models.MailSendIntent.org_id == self.org_id,
            )
        ).scalars().first()
        if intent is None:
            raise NotFound(f"no send intent {intent_id} in this organisation")
        return intent

    def _job(self, job_id: str) -> models.Job:
        job = self.db.execute(
            select(models.Job).where(
                models.Job.id == job_id, models.Job.org_id == self.org_id
            )
        ).scalars().first()
        if job is None:
            raise NotFound(f"no job {job_id} in this organisation")
        return job


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
COMMANDS = (
    "list-stuck-workflows", "show-workflow", "show-job", "show-agent",
    "retry-step", "requeue-job", "resume-workflow", "cancel-workflow",
    "pause-agent", "resume-agent", "drain-fleet",
    # Phase 7b outbound recovery. Note what is absent: there is no `force-send` and
    # no `send-without-approval`, because administrative recovery must never be a
    # way around the approval the whole phase depends on.
    "show-send-intent", "show-send-attempts", "cancel-send", "reconcile-send",
    "retry-confirmed-not-sent",
)


def run_command(
    db: Session, org_id: str, actor_id: str, argv: list[str]
) -> CommandResult:
    """Dispatch one CLI invocation. Returns a result rather than printing, so the
    caller owns presentation and tests can assert on structure."""
    if not argv:
        raise Refused(f"usage: <command> [args]. Commands: {', '.join(COMMANDS)}")
    name, *rest = argv
    commands = AdminCommands(db, org_id, actor_id=actor_id)

    table = {
        "list-stuck-workflows": lambda: commands.list_stuck_workflows(),
        "show-workflow": lambda: commands.show_workflow(rest[0]),
        "show-job": lambda: commands.show_job(rest[0]),
        "show-agent": lambda: commands.show_agent(rest[0] if rest else None),
        "retry-step": lambda: commands.retry_step(rest[0]),
        "requeue-job": lambda: commands.requeue_job(rest[0]),
        "resume-workflow": lambda: commands.resume_workflow(rest[0]),
        "cancel-workflow": lambda: commands.cancel_workflow(rest[0], reason=" ".join(rest[1:])),
        "pause-agent": lambda: commands.pause_agent(reason=" ".join(rest)),
        "resume-agent": lambda: commands.resume_agent(),
        "drain-fleet": lambda: commands.drain_fleet(),
        "show-send-intent": lambda: commands.show_send_intent(rest[0]),
        "show-send-attempts": lambda: commands.show_send_attempts(rest[0]),
        "cancel-send": lambda: commands.cancel_send(rest[0], reason=" ".join(rest[1:])),
        "reconcile-send": lambda: commands.reconcile_send(rest[0]),
        "retry-confirmed-not-sent": lambda: commands.retry_confirmed_not_sent(rest[0]),
    }
    handler = table.get(name)
    if handler is None:
        raise Refused(f"unknown command {name!r}. Commands: {', '.join(COMMANDS)}")
    return handler()


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - process entry
    """``python -m agent.admin --org <id> --actor <id> <command> [args]``"""
    import argparse
    import json

    from config import settings
    from database import SessionLocal
    from observability import configure_logging, register_secrets_from_settings

    parser = argparse.ArgumentParser(description="Granada agent recovery commands")
    parser.add_argument("--org", required=True, help="organisation id")
    parser.add_argument("--actor", required=True, help="operator id, recorded in the audit trail")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    configure_logging(level=getattr(settings, "log_level", "INFO"), service="granada-admin")
    register_secrets_from_settings(settings)

    db = SessionLocal()
    try:
        result = run_command(db, args.org, args.actor, args.command)
        db.commit()
        print(json.dumps(result.as_dict(), indent=2, default=str))
        return 0 if result.ok else 1
    except (AdminError, Refused, NotFound, SpecialistError, AgentError) as exc:
        db.rollback()
        print(json.dumps({"ok": False, "refused": True, "detail": str(exc)}, indent=2))
        return 2
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
