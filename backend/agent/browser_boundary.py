"""The browser-execution boundary: what a future worker receives, and what it may not touch.

The directive's §12. **No browser engine is installed, deployed or enabled by this module.** It
defines the contract so the next stage has a place to land, and so the isolation properties are
decided by design rather than discovered under load.

WHY A BOUNDARY AND NOT AN ADAPTER
`agent/submission/` already holds the provider protocols (`SubmissionProvider`, `HandoffBuilder`).
A browser worker is a *different* thing from a submission provider: it is a process that holds
credentials, opens sessions and drives a third party's website, and the risk is not "does it submit"
but "what can it reach". So this module defines a task and a result, and states the isolation rules
positively - each as something a caller can enforce mechanically rather than trust.

FOLLOWS `agent/decision/`'s CONVENTION
`decision/` splits exceptions (`NoDecisionProvider`, `DecisionProviderUnavailable`,
`DecisionProviderError`) from the gateway. The same shape here: `BrowserWorkerUnavailable` when no
worker is configured, `BrowserTaskRefused` when a task would breach isolation, and a protocol the
gateway implements. Reusing the convention means an operator who understands one understands both.

THE THREE RULES, EACH ENFORCEABLE

1. A TASK NAMES ONE ORGANISATION, AND ONLY DOCUMENTS FROM IT. `BrowserTask` carries `org_id`, and
   `validate_task()` refuses a task whose document references do not all belong to that organisation.
   Cross-tenant reach is a refusal at construction, not a check at execution.

2. CREDENTIALS ARE REFERENCES, NEVER VALUES. The task carries credential *names* resolved by the
   worker from the organisation's own store. Putting a password in a task object means it lands in
   the job payload, the audit log and the retry history - three places nobody intended.

3. THE ACTION SCOPE IS DECLARED AND BOUNDED. A task says which portal and which path, and the worker
   is not permitted to navigate elsewhere. "Do not assume CAPTCHA or access-denied is universally
   bypassable" is the directive's own words: a worker that hits one reports `BLOCKED_EXTERNAL` and
   stops, because an agent that improvises its way past an access control is not doing authorisation
   work - it is doing the opposite.

RECOVERY IS PART OF THE CONTRACT
`checkpoint` is an opaque token the worker persists after each completed step, and a resumed task
carries it back. Bounded retries: `max_attempts` with the existing `retry_not_before` convention, so
a portal that is down parks rather than spins - the runaway-loop defect this project has already
fixed once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable

import models


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BrowserTaskRefused(RuntimeError):
    """A task was constructed that breaches tenant or scope isolation.

    An exception rather than a refusal result, because a task object that should not exist should not
    reach a worker at all - a value the caller can log and continue past is the wrong shape for
    something that would open another organisation's document.
    """


class BrowserWorkerUnavailable(RuntimeError):
    """No browser worker is configured.

    Raised rather than returning a blocked result, so a caller cannot mistake "no worker exists" for
    "the worker tried and could not". Those need different responses: one is an operator task, the
    other is a portal problem.
    """


class BrowserOutcome(str, Enum):
    """What a worker reports. Deliberately no SUCCESS."""

    #: Every step completed and the portal returned a reference. The ONLY value that means submitted.
    CONFIRMED = "CONFIRMED"
    #: Steps completed but the portal gave no reference. NOT submitted - the same distinction
    #: `SubmissionPackage` makes when it refuses SUBMITTED without a receipt.
    UNCONFIRMED = "UNCONFIRMED"
    #: A CAPTCHA, an access denial or a required human step. The worker STOPPED.
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    #: Something recoverable: a timeout, a changed page, a dropped session.
    RECOVERABLE = "RECOVERABLE"
    #: Unrecoverable.
    FAILED = "FAILED"


@dataclass
class CredentialRef:
    """A credential BY NAME. Never a value.

    `purpose` is recorded because a worker holding an organisation's portal login should be able to
    state what it was for; a reference with no stated purpose cannot be reviewed.
    """

    name: str
    purpose: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "purpose": self.purpose}


@dataclass
class ActionScope:
    """Where the worker may go, and nowhere else.

    `allowed_hosts` is exact-match, not suffix-match: `evil-funder.example` must not match
    `funder.example` by ending with it, which is the oldest bug in allow-listing.
    """

    portal_name: str
    allowed_hosts: tuple[str, ...]
    #: Form paths within the portal the worker may visit. Empty means the portal root only.
    allowed_path_prefixes: tuple[str, ...] = ()
    max_steps: int = 200
    #: Whether loopback targets are permitted for THIS task.
    #:
    #: False by default and deliberately so. The controlled test portal runs on 127.0.0.1 and has to
    #: be testable, but a production task must never inherit that permission by being constructed the
    #: same way - so it is an explicit opt-in on the scope rather than a global switch or a default.
    allow_loopback: bool = False

    def permits(self, host: str, path: str) -> bool:
        if host not in self.allowed_hosts:
            return False
        if not self.allowed_path_prefixes:
            return path in ("", "/")
        return any(path.startswith(prefix) for prefix in self.allowed_path_prefixes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "portal_name": self.portal_name,
            "allowed_hosts": list(self.allowed_hosts),
            "allowed_path_prefixes": list(self.allowed_path_prefixes),
            "max_steps": self.max_steps,
        }


@dataclass
class BrowserTask:
    """Everything a worker receives. There is no twenty-first field for "whatever else it needs"."""

    task_id: str
    org_id: str
    package_id: str
    workflow_id: Optional[str]
    job_id: Optional[str]

    package_fingerprint: str
    action_scope: ActionScope
    credentials: list[CredentialRef] = field(default_factory=list)

    #: Form-ready values, drawn from VERIFIED organisation facts. A worker must not be handed a value
    #: the platform could not substantiate, because it would type it into a funder's form.
    form_data: dict[str, Any] = field(default_factory=dict)

    #: Document references, each `{"document_id", "doc_type", "checksum_sha256"}`. Identifiers, not
    #: paths: the worker resolves them through the same authorised vault the rest of the platform
    #: uses, so a task cannot smuggle a filesystem path to somewhere else.
    documents: list[dict[str, Any]] = field(default_factory=list)

    #: Opaque progress token from a previous attempt, if this is a resume.
    checkpoint: Optional[str] = None
    max_attempts: int = 3
    created_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "org_id": self.org_id,
            "package_id": self.package_id,
            "workflow_id": self.workflow_id,
            "job_id": self.job_id,
            "package_fingerprint": self.package_fingerprint,
            "action_scope": self.action_scope.as_dict(),
            "credentials": [c.as_dict() for c in self.credentials],
            "form_data": self.form_data,
            "documents": self.documents,
            "checkpoint": self.checkpoint,
            "max_attempts": self.max_attempts,
            "created_at": (self.created_at or _now()).isoformat(),
        }


@dataclass
class BrowserResult:
    """What a worker returns. Every field the directive lists, and no optimistic defaults."""

    task_id: str
    outcome: BrowserOutcome
    completed_steps: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    recoverable_problems: list[str] = field(default_factory=list)
    #: The portal's own reference. Present ONLY when the funder returned one - this is the field that
    #: distinguishes a submission from an attempt, and nothing may fabricate it.
    submission_identifier: Optional[str] = None
    evidence: list[str] = field(default_factory=list)
    checkpoint: Optional[str] = None
    occurred_at: Optional[datetime] = None
    audit: dict[str, Any] = field(default_factory=dict)

    @property
    def submitted(self) -> bool:
        """True only with a CONFIRMED outcome AND an identifier.

        Both conditions: a worker that reports CONFIRMED with no reference has not been told by the
        funder that anything arrived, and treating it as submitted is how an organisation believes it
        applied when it did not.
        """
        return self.outcome == BrowserOutcome.CONFIRMED and bool(self.submission_identifier)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "outcome": self.outcome.value,
            "submitted": self.submitted,
            "completed_steps": self.completed_steps,
            "validation": self.validation,
            "recoverable_problems": self.recoverable_problems,
            "submission_identifier": self.submission_identifier,
            "evidence": self.evidence,
            "checkpoint": self.checkpoint,
            "occurred_at": (self.occurred_at or _now()).isoformat(),
            "audit": self.audit,
        }


@runtime_checkable
class BrowserWorker(Protocol):
    """The gateway a worker implements. Three methods, matching `decision/`'s gateway shape."""

    def capabilities(self) -> set[str]: ...

    def execute(self, task: BrowserTask) -> BrowserResult: ...

    def resume(self, task: BrowserTask) -> BrowserResult: ...


def validate_task(task: BrowserTask, *, org_document_ids: set[str]) -> None:
    """Refuse a task that breaches isolation. Call before handing anything to a worker.

    `org_document_ids` is the set of document ids the task's organisation actually owns, resolved by
    the CALLER through the authorised vault. This function cannot look them up itself - doing so would
    require the cross-tenant privilege the directive forbids the worker from having.
    """
    if not task.org_id:
        raise BrowserTaskRefused("a browser task must name an organisation")

    for document in task.documents:
        document_id = str(document.get("document_id") or "")
        if not document_id:
            raise BrowserTaskRefused("a document reference has no document_id")
        if document_id not in org_document_ids:
            # THE isolation check. A reference to another organisation's document is refused here
            # rather than resolved and filtered later, because later is where it leaks.
            raise BrowserTaskRefused(
                f"document {document_id[:8]} does not belong to organisation "
                f"{task.org_id[:8]}; refusing to build the task"
            )

    if not task.action_scope.allowed_hosts:
        raise BrowserTaskRefused("a task must declare at least one allowed host")

    if task.max_attempts < 1:
        raise BrowserTaskRefused("max_attempts must be at least 1")

    for host in task.action_scope.allowed_hosts:
        if host.startswith("*") or host.startswith("."):
            # Suffix matching lets `notfunder.example` match. Exact hosts only.
            raise BrowserTaskRefused(
                f"wildcard host {host!r} is not permitted; allowed hosts must be exact"
            )

    # SSRF AND INTERNAL-NETWORK PROTECTION (§8). Exact-match is not enough on its own: the allow-list
    # is CONFIGURATION, and the controlled test portal genuinely needs `127.0.0.1` to be testable. So
    # the hosts are screened on what they RESOLVE to, here at BUILD time - when the reason can still
    # reach whoever wrote the configuration - and again by the runtime.
    from .target_guard import screen_hosts, check_target

    allow_loopback = bool(getattr(task.action_scope, "allow_loopback", False))
    refused = screen_hosts(
        task.action_scope.allowed_hosts, allow_loopback=allow_loopback
    )
    if refused:
        host, why = refused[0]
        raise BrowserTaskRefused(
            f"allowed host {host!r} is not a permitted target: {why}"
        )

    target = getattr(task, "target_url", "") or ""
    if target:
        decision = check_target(target, allow_loopback=allow_loopback, resolve=False)
        if not decision.permitted:
            raise BrowserTaskRefused(
                f"target_url is not a permitted target: {decision.because}"
            )


def build_task(
    db: Any,
    package: models.SubmissionPackage,
    *,
    action_scope: ActionScope,
    form_data: Optional[dict[str, Any]] = None,
    credentials: Optional[list[CredentialRef]] = None,
    checkpoint: Optional[str] = None,
    max_attempts: int = 3,
) -> BrowserTask:
    """Assemble a task for one package.

    REFUSES to build one for a package that is not READY. A worker must not be given work that the
    readiness engine has said cannot be sent - the gate exists so that an unready package cannot be
    submitted by any route, including this one.
    """
    from agent import readiness as readiness_engine

    verdict = readiness_engine.evaluate_by_id(db, str(package.id))
    if verdict is not None and not verdict.is_ready:
        raise BrowserTaskRefused(
            f"package {str(package.id)[:8]} is not ready: {verdict.message()}"
        )

    manifest = package.manifest or {}
    documents = [
        {
            "document_id": str(d.get("document_id")),
            "doc_type": d.get("doc_type"),
            "checksum_sha256": d.get("checksum_sha256"),
        }
        for d in (manifest.get("documents") or [])
        if d.get("document_id")
    ]

    return BrowserTask(
        task_id=str(package.id),
        org_id=str(package.org_id),
        package_id=str(package.id),
        workflow_id=None,
        job_id=None,
        package_fingerprint=str(package.package_fingerprint),
        action_scope=action_scope,
        credentials=list(credentials or []),
        # Verified form data only. An empty dict is the honest default: the platform does not
        # fabricate a value to make a form look complete.
        form_data=dict(form_data or {}),
        documents=documents,
        checkpoint=checkpoint,
        max_attempts=max_attempts,
        created_at=_now(),
    )


def describe_integration() -> str:
    """The operator-facing summary, and the reason nothing is installed.

    Recorded here rather than only in a report, so the next person to look for the browser engine
    finds this instead of an empty module and assumes it was forgotten.
    """
    return (
        "No browser worker is installed or enabled. Granada prepares application packages and a "
        "person submits them. A browser worker would receive a BrowserTask, resolve credentials from "
        "the organisation's own store, drive one allow-listed portal, checkpoint after each step and "
        "return a BrowserResult - and could not reach any other organisation's documents, because "
        "validate_task refuses the task."
    )
