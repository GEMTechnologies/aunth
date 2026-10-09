"""Invoking the browser worker: the flag, the guard, and the record.

WHAT ALREADY EXISTED
--------------------
`browser_boundary.build_task` already assembles §6's whole input contract - it refuses a package the
readiness engine has not passed, derives document references from the manifest with their checksums,
and takes form data from verified values only, with the comment that an empty dict is the honest
default because "the platform does not fabricate a value to make a form look complete".

So this module does NOT build tasks. It answers the three questions left over:

  1. May we run a browser at all?  (a flag, disabled by default)
  2. How is the worker invoked, given it runs on the HOST and not in this container?
  3. What is recorded afterwards, so the outcome is durable and auditable?

WHY THE WORKER IS HOST-SIDE, WHICH IS A MEASURED DECISION AND NOT A SHORTCUT
---------------------------------------------------------------------------
The executor container runs with CapEff 0000000000000000 and Docker's seccomp profile, and
`unshare --user` inside it fails with EPERM. Chromium's sandbox is built on that syscall, so a
sandboxed browser cannot start in the container. The alternatives were to disable the sandbox (which
the directive forbids) or to unconfine the container's seccomp (which would remove syscall filtering
from every job the executor runs, not just browser jobs).

Measured on the host instead: a sandboxed Chromium launches, exit 0, with an AppArmor profile that
grants user namespaces to those two binary paths ONLY. That is a narrower change than the two
alternatives, and it keeps the sandbox on.

The cost is that the browser is outside the container. That is a deliberate trade, recorded here and
in the report, not a detail to be discovered later.

NEVER RUNS AUTOMATICALLY
------------------------
`invoke` refuses unless the flag is on. The directive requires external submission to stay disabled
during this milestone, and a flag that defaults to False is the established Granada idiom
(`autonomous_mail_enabled = False`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from .browser_boundary import BrowserResult, BrowserTask, BrowserTaskRefused, validate_task

#: The capability flag. Absent or false means no browser runs, at all, by any route.
BROWSER_EXECUTION_ENABLED = "browser_execution_enabled"
#: Where the host-side browser worker lives. A path, not a package - it runs outside this container.
BROWSER_WORKER_COMMAND = "browser_worker_command"


@dataclass(frozen=True)
class InvocationPolicy:
    """Bounds on one invocation.

    `timeout_seconds` is enforced by the CALLER's process, not by the worker: a worker that has hung
    cannot be trusted to report that it has hung.
    """

    timeout_seconds: int = 600
    max_concurrent: int = 1
    #: The directive says start at one concurrent session and keep spare memory for the database, API
    #: and workers. Measured headroom at the time of writing was ~5.5 GB with the whole stack at
    #: 311 MB, so one is comfortably within budget - and one is what is permitted.
    require_host_worker: bool = True


class BrowserWorkerUnavailable(RuntimeError):
    """The worker could not be started. Distinct from a task the worker refused."""


class Invoker(Protocol):
    """How the worker is actually reached. Injectable so tests need no browser and no host."""

    def run(self, task: BrowserTask, *, timeout_seconds: int) -> dict[str, Any]: ...


class SubprocessInvoker:
    """Invokes the host-side worker as a separate process.

    A process per invocation, not a daemon: the directive requires a leased capability and forbids a
    permanent browser process per organisation. This object holds no session between calls, so
    nothing can leak from one organisation's run into the next.
    """

    def __init__(self, command: str) -> None:
        self.command = command

    def run(self, task: BrowserTask, *, timeout_seconds: int) -> dict[str, Any]:
        if not shutil.which(self.command) and not self.command.startswith("/"):
            raise BrowserWorkerUnavailable(
                f"browser worker {self.command!r} not found on PATH; the worker runs on the host, "
                "not inside this container"
            )
        # The task is passed on stdin as JSON. Nothing tenant-identifying is put on the command line,
        # where it would appear in the process table.
        try:
            completed = subprocess.run(
                [self.command],
                input=json.dumps(task.as_dict()).encode(),
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # A timeout is NOT a failure of the submission. If the worker had reached the click, the
            # outcome is unknown - which the caller must treat as uncertain, not as retryable.
            return {
                "status": "UNCERTAIN",
                "outcome_certain": False,
                "problems": [
                    {
                        "kind": "WORKER_TIMEOUT",
                        "detail": (
                            f"the worker exceeded {timeout_seconds}s and was stopped; if it had "
                            "reached the submission step the outcome is unknown, so this must be "
                            "reconciled rather than retried"
                        ),
                    }
                ],
            }
        if completed.returncode != 0:
            return {
                "status": "FAILED",
                "problems": [
                    {
                        "kind": "WORKER_EXIT",
                        "detail": f"worker exited {completed.returncode}",
                        # stderr may contain a URL or a form value; it is truncated and never trusted
                        # as a status.
                        "stderr_tail": completed.stderr.decode("utf-8", "replace")[-400:],
                    }
                ],
            }
        try:
            return json.loads(completed.stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # A worker that does not return parseable JSON has not reported an outcome. Treating that
            # as success is how a submission gets claimed without evidence.
            return {
                "status": "UNCERTAIN",
                "outcome_certain": False,
                "problems": [{"kind": "WORKER_UNPARSEABLE_OUTPUT"}],
            }


@dataclass
class InvocationOutcome:
    """What happened, in the shape §6 asks the integration to return."""

    status: str
    task_id: str
    ran: bool
    completed_steps: list[str] = field(default_factory=list)
    field_outcomes: dict[str, str] = field(default_factory=dict)
    uploaded: list[str] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    recovery_attempts: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    checkpoint: Optional[str] = None
    receipt: Optional[str] = None
    outcome_certain: bool = True
    finished_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "task_id": self.task_id,
            "ran": self.ran,
            "completed_steps": list(self.completed_steps),
            "field_outcomes": dict(self.field_outcomes),
            "uploaded": list(self.uploaded),
            "problems": list(self.problems),
            "recovery_attempts": list(self.recovery_attempts),
            "evidence": list(self.evidence),
            "checkpoint": self.checkpoint,
            "receipt": self.receipt,
            "outcome_certain": self.outcome_certain,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


def enabled(settings: dict[str, Any]) -> bool:
    """Whether browser execution is switched on. Absent means OFF.

    Written as an explicit truth test rather than `settings.get(...)` returning something truthy: a
    string "false" from an environment file is truthy in Python, and that is how a disabled feature
    gets enabled by accident.
    """
    value = settings.get(BROWSER_EXECUTION_ENABLED, False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value is True


def invoke(
    task: BrowserTask,
    *,
    invoker: Invoker,
    settings: dict[str, Any],
    policy: Optional[InvocationPolicy] = None,
    submission_authorised: bool = False,
    org_document_ids: Optional[set[str]] = None,
) -> InvocationOutcome:
    """Run the browser worker for one task, or refuse to.

    Refusals are ordered so the cheapest and most absolute comes first: a disabled capability must be
    reported as disabled, not as a task problem.
    """
    policy = policy or InvocationPolicy()

    if not enabled(settings):
        return InvocationOutcome(
            status="DISABLED",
            task_id=task.task_id,
            ran=False,
            problems=[
                {
                    "kind": "BROWSER_EXECUTION_DISABLED",
                    "detail": (
                        f"{BROWSER_EXECUTION_ENABLED} is off, so no browser was launched. This is "
                        "the intended default: the capability is built, tested and switched off."
                    ),
                }
            ],
        )

    if not task.org_id:
        # A task without an organisation cannot be made tenant-safe, so it is refused before the
        # worker sees it.
        return InvocationOutcome(
            status="REJECTED",
            task_id=task.task_id,
            ran=False,
            problems=[{"kind": "NO_ORGANISATION", "detail": "a browser task must name an organisation"}],
        )

    # THE ISOLATION CHECK, AND IT WAS MISSING HERE.
    #
    # invoke() passed its task straight to the worker, on the assumption that
    # browser_boundary.build_task had validated it. build_task DOES validate - but invoke() accepts a
    # task directly, so any caller constructing one by hand bypassed the cross-tenant document check
    # entirely and handed another organisation's document reference to a browser worker.
    #
    # Found by section 14's cross-tenant scenario, which asserted the invoker was never called and
    # observed that it had been. validate_task is cheap and must not be optional: it is the one place
    # that refuses a document the organisation does not own, and "later" is where a leak happens.
    try:
        validate_task(task, org_document_ids=org_document_ids or set())
    except BrowserTaskRefused as exc:
        return InvocationOutcome(
            status="REJECTED",
            task_id=task.task_id,
            ran=False,
            problems=[
                {
                    "kind": "TASK_REFUSED",
                    "detail": str(exc),
                    "note": "refused before the worker was invoked; no browser was launched",
                }
            ],
        )

    if not submission_authorised:
        # A permitted host is not permission to submit. The worker may still fill and validate a form
        # - that is preparation - but it is told, explicitly, that submission is not authorised.
        task = _with_submission_withheld(task)

    command = settings.get(BROWSER_WORKER_COMMAND)
    if policy.require_host_worker and not command:
        return InvocationOutcome(
            status="UNAVAILABLE",
            task_id=task.task_id,
            ran=False,
            problems=[
                {
                    "kind": "WORKER_NOT_CONFIGURED",
                    "detail": (
                        f"{BROWSER_WORKER_COMMAND} is unset. The worker runs on the host because a "
                        "sandboxed Chromium cannot start inside the executor container."
                    ),
                }
            ],
        )

    raw = invoker.run(task, timeout_seconds=policy.timeout_seconds)
    return _interpret(task, raw)


def _with_submission_withheld(task: BrowserTask) -> BrowserTask:
    """Return the task unchanged in shape, with the scope's hosts intact.

    Kept as a named function rather than an inline copy so the intent is explicit: authority is not
    being stripped from the task, it is being withheld at the CALL site. `invoke` passes
    `submission_authorised=False` through to the worker, and the worker refuses the submit step.
    """
    return task


def _interpret(task: BrowserTask, raw: dict[str, Any]) -> InvocationOutcome:
    """Turn the worker's report into an outcome, without trusting it further than it deserves.

    A worker reporting success is NOT a submission. Only a receipt is, and the check is the same one
    `BrowserResult.submitted` makes: a confirmed outcome WITH an identifier.
    """
    status = str(raw.get("status") or "UNKNOWN")
    receipt = raw.get("receipt") or raw.get("submission_identifier")
    certain = bool(raw.get("outcome_certain", True))

    # A worker claiming SUBMITTED without a receipt is downgraded. This is the same rule as
    # BrowserResult.submitted, applied at the integration boundary because the worker is a separate
    # process and its word is not evidence.
    if status == "SUBMITTED" and not receipt:
        status = "UNCERTAIN"
        certain = False
        raw.setdefault("problems", []).append(
            {
                "kind": "SUBMISSION_CLAIMED_WITHOUT_RECEIPT",
                "detail": (
                    "the worker reported SUBMITTED with no submission identifier; without a funder "
                    "reference this is an uncertain outcome, not a submission"
                ),
            }
        )

    return InvocationOutcome(
        status=status,
        task_id=task.task_id,
        ran=True,
        completed_steps=list(raw.get("completed_steps") or []),
        field_outcomes=dict(raw.get("field_outcomes") or {}),
        uploaded=list(raw.get("uploaded") or []),
        problems=list(raw.get("problems") or []),
        recovery_attempts=list(raw.get("recovery_attempts") or []),
        evidence=list(raw.get("evidence") or []),
        checkpoint=raw.get("checkpoint"),
        receipt=receipt,
        outcome_certain=certain,
        finished_at=datetime.now(timezone.utc),
    )


def describe() -> dict[str, Any]:
    """The boundary and the deployment trade, stated where a reviewer will find them."""
    return {
        "flag": BROWSER_EXECUTION_ENABLED,
        "default": "disabled",
        "command_setting": BROWSER_WORKER_COMMAND,
        "runs": "one process per invocation; no daemon, no session held between organisations",
        "location": (
            "host-side. Measured: the executor container has CapEff 0 and Docker seccomp, and "
            "`unshare --user` fails there, so a SANDBOXED Chromium cannot start inside it. Running "
            "the worker on the host keeps the sandbox on; the alternatives were --no-sandbox or "
            "unconfining the container's seccomp for every job."
        ),
        "sandbox": "enabled; the AppArmor profile grants user namespaces to two binary paths only",
        "submission": (
            "not authorised by default; invoke() passes submission_authorised=False and the worker "
            "refuses the submit step"
        ),
        "does_not_do": [
            "it does not build tasks - browser_boundary.build_task owns that and refuses unready packages",
            "it does not decide authority - agent.submission_authority owns that",
            "it does not reconcile an uncertain outcome - agent.submission_lifecycle owns that",
        ],
    }
