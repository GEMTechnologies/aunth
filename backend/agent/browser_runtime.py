"""The browser execution cycle: OBSERVE -> REASON -> PLAN -> ACT -> VERIFY -> CHECKPOINT.

WHY A CYCLE AND NOT A SCRIPT
----------------------------
A scripted browser driver encodes what the page looked like when the script was written. Grant
portals change their forms, reorder fields and rename labels, so a script that worked becomes a
script that silently fills the wrong box. The cycle instead grounds every action in the page as it is
right now, which is what makes layout variation survivable - and the portal fixture's `?variant=b`
exists to prove the difference rather than assert it.

WHAT THIS MODULE IS, AND IS NOT
-------------------------------
It is the ENGINE: the loop, the bounded recovery, the checkpointing, the outcome recording. It is
NOT the browser. A provider (`StagehandProvider`, `BrowserUseProvider`, or a fake in tests) does the
observing and acting; this module decides what to do next and refuses to do some of it.

It consumes `agent.browser_boundary` rather than redefining it. That module already owns the task
shape, the exact-host action scope, the credential reference and the submitted/not-submitted rule -
including that `BrowserResult.submitted` is True only on a CONFIRMED outcome WITH an identifier. None
of that is duplicated here.

THE RECOVERY RULE THAT MATTERS
------------------------------
The directive: "If an action fails, determine why before retrying." and "do not repeatedly click,
refresh or submit without a bounded recovery strategy."

So a retry requires a CLASSIFIED reason. `RetryPolicy` refuses to retry an unclassified failure, caps
attempts per step, caps total actions, and - critically - NEVER retries an action that might already
have had an external effect. A `submit` click that timed out is not retried here; it becomes an
uncertain outcome for `submission_lifecycle` to reconcile, which is the whole point of that module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Protocol

from .browser_boundary import (
    ActionScope,
    BrowserResult,
    BrowserTask,
    BrowserTaskRefused,
    validate_task,
)

# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------
OBSERVE = "OBSERVE"
REASON = "REASON"
PLAN = "PLAN"
ACT = "ACT"
VERIFY = "VERIFY"
CHECKPOINT = "CHECKPOINT"

CYCLE = (OBSERVE, REASON, PLAN, ACT, VERIFY, CHECKPOINT)


class Step(str, Enum):
    """What the engine decided to do next. A closed set, so an unexpected value is a bug and not a
    silent no-op."""

    NAVIGATE = "NAVIGATE"
    FILL = "FILL"
    UPLOAD = "UPLOAD"
    CLICK = "CLICK"
    DECLARE = "DECLARE"
    SUBMIT = "SUBMIT"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    UNCERTAIN = "UNCERTAIN"
    FAILED = "FAILED"


#: Actions that may have had an EXTERNAL effect. Never retried automatically, and never repeated
#: after an ambiguous failure - the reason `submission_lifecycle` has an UNCERTAIN state.
CONSEQUENTIAL = frozenset({Step.SUBMIT})

#: How a failure is classified, because the classification decides whether a retry is allowed.
class Failure(str, Enum):
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    ELEMENT_CHANGED = "ELEMENT_CHANGED"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    HUMAN_VERIFICATION = "HUMAN_VERIFICATION"
    ACCESS_DENIED = "ACCESS_DENIED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    UNKNOWN = "UNKNOWN"


#: Which failures a bounded retry is permitted for. Everything absent is refused - notably
#: HUMAN_VERIFICATION (a CAPTCHA is not to be circumvented), ACCESS_DENIED (403 is an answer, not an
#: obstacle) and UNKNOWN (you cannot retry what you have not diagnosed).
RETRYABLE = frozenset({Failure.TRANSIENT_NETWORK, Failure.ELEMENT_CHANGED})


class BrowserError(Exception):
    """Raised for a refused action. Never swallowed by the engine: a refusal is a decision."""


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded, and refusing by default.

    `max_attempts_per_step` exists so a single stubborn field cannot consume the run, and
    `max_total_actions` so a page that keeps producing new work cannot either.
    """

    max_attempts_per_step: int = 3
    max_total_actions: int = 60
    #: A whole-run budget. The directive asks for an execution deadline, and a run that ignores it
    #: holds a browser session that another organisation's job is waiting for.
    max_duration: timedelta = timedelta(minutes=10)
    #: Longer waits stop being "the page is still loading" and start being a hang.
    backoff_base: timedelta = timedelta(seconds=1)


# ---------------------------------------------------------------------------
# The provider boundary
# ---------------------------------------------------------------------------
@dataclass
class PageState:
    """What the browser can see. Deliberately structural rather than raw HTML.

    A provider returns the visible form shape, not the document: the reasoning step needs fields and
    labels, and handing a model an entire page of untrusted markup is how prompt injection gets a
    hearing. `untrusted_text` is carried separately and is never treated as instruction.
    """

    url: str
    title: str = ""
    #: name -> {label, type, required, options}
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Visible validation messages, as the portal rendered them.
    validation_messages: list[str] = field(default_factory=list)
    #: Buttons and links by visible text.
    controls: list[str] = field(default_factory=list)
    #: Text from the page that must NEVER be interpreted as an instruction to Granada.
    untrusted_text: str = ""


@dataclass
class ActionResult:
    ok: bool
    failure: Optional[Failure] = None
    detail: str = ""
    page: Optional[PageState] = None


class BrowserProvider(Protocol):
    """What an adapter must implement. Small on purpose: everything provider-specific lives behind
    these five calls, so swapping Stagehand for Browser Use is a new class and not a rewrite."""

    name: str

    def launch(self, *, profile_dir: str, headless: bool = True) -> None: ...

    def close(self) -> None: ...

    def observe(self) -> PageState: ...

    def act(self, step: Step, target: str, value: Optional[str] = None) -> ActionResult: ...

    def screenshot(self) -> str: ...


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
@dataclass
class PlannedAction:
    step: Step
    target: str
    value: Optional[str] = None
    #: Why the engine chose this, recorded so a run can be explained without replaying it.
    because: str = ""


def plan_next(
    page: PageState,
    *,
    values: dict[str, str],
    uploads: dict[str, str],
    already_done: set[str],
    declaration_accepted: bool,
) -> PlannedAction:
    """Decide the next action from the page AS IT IS.

    This is what makes layout variation survivable: nothing here matches a fixed selector or
    position. Fields are located by their `name` and understood through their `label`, so reordering
    and relabelling do not change the plan.

    Returns DONE / BLOCKED / UNCERTAIN when no action is warranted, rather than a best guess.
    """
    # 1. Fill anything the page asks for that we have verified data for and have not filled.
    for name, spec in page.fields.items():
        if name in already_done:
            continue
        if spec.get("type") in ("file",):
            if name in uploads:
                return PlannedAction(Step.UPLOAD, name, uploads[name], f"{name} is an upload")
            if spec.get("required"):
                # A required upload with no document held is MISSING INFORMATION, not a step to skip.
                # `continue` used to fall through to DONE, so a form that could never be sent reported
                # COMPLETED - found by the section 14 upload scenario.
                return PlannedAction(
                    Step.BLOCKED, name, None,
                    f"the page requires {name} and Granada holds no authorised document for it",
                )
            continue
        if spec.get("type") in ("checkbox", "radio"):
            continue
        if name in values and values[name]:
            return PlannedAction(Step.FILL, name, values[name], f"page asks for {name}")

    # 2. Conditional fields the page is SHOWING but we have no value for. The page asks; we answer
    #    only if we hold a verified value, otherwise this is missing information, not a guess.
    for name, spec in page.fields.items():
        if name in already_done:
            continue
        if spec.get("required") and name not in values and spec.get("type") not in ("file", "checkbox", "radio"):
            return PlannedAction(
                Step.BLOCKED, name, None, f"the page requires {name} and Granada holds no verified value"
            )

    # 3. Declarations. A declaration is a legal statement: it is accepted only when the package
    #    carries authority to make it, never inferred from silence.
    if not declaration_accepted:
        for control in page.controls:
            low = control.lower()
            if "declar" in low or "certify" in low or "confirm" in low:
                return PlannedAction(Step.DECLARE, control, None, "the form requires a declaration")

    # 4. Validation messages mean the previous action did not satisfy the page. Report, do not
    #    blindly resubmit.
    if page.validation_messages:
        return PlannedAction(
            Step.BLOCKED, "", None, f"the page rejected the submission: {'; '.join(page.validation_messages[:3])}"
        )

    # 5. Submit only when the page is offering it and nothing is outstanding.
    for control in page.controls:
        if "submit" in control.lower() or "send application" in control.lower():
            return PlannedAction(Step.SUBMIT, control, None, "the form is complete and offers submission")

    return PlannedAction(Step.DONE, "", None, "no further action is warranted on this page")


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
@dataclass
class Checkpoint:
    """Persisted progress. Exists so a killed worker resumes rather than restarts - and so a run
    that stopped before any consequential action can be told apart from one that did not."""

    at: datetime
    cycle_step: str
    page_url: str
    completed: list[str] = field(default_factory=list)
    #: True once a consequential action has been ATTEMPTED, whether or not it succeeded. This is the
    #: flag that forbids a restart.
    consequential_attempted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "cycle_step": self.cycle_step,
            "page_url": self.page_url,
            "completed": list(self.completed),
            "consequential_attempted": self.consequential_attempted,
        }


@dataclass
class RunReport:
    """What happened, in the shape the directive asks the browser boundary to return."""

    status: str
    completed_steps: list[str] = field(default_factory=list)
    field_outcomes: dict[str, str] = field(default_factory=dict)
    uploaded: list[str] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    recovery_attempts: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    receipt: Optional[str] = None
    #: Distinguishes "we know nothing was submitted" from "we do not know".
    outcome_certain: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "completed_steps": list(self.completed_steps),
            "field_outcomes": dict(self.field_outcomes),
            "uploaded": list(self.uploaded),
            "problems": list(self.problems),
            "recovery_attempts": list(self.recovery_attempts),
            "evidence": list(self.evidence),
            "checkpoints": list(self.checkpoints),
            "receipt": self.receipt,
            "outcome_certain": self.outcome_certain,
        }


class BrowserRuntime:
    """Drives one leased browser session for one organisation's one task.

    One runtime, one session, one tenant. Not a pool and not a daemon: the directive requires a
    leased capability, and this object's lifetime IS the lease.
    """

    def __init__(
        self,
        provider: BrowserProvider,
        *,
        policy: Optional[RetryPolicy] = None,
        now: Optional[Any] = None,
    ) -> None:
        self.provider = provider
        self.policy = policy or RetryPolicy()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._attempts: dict[str, int] = {}
        self._actions = 0
        self._started: Optional[datetime] = None
        self._checkpoints: list[Checkpoint] = []

    # -- budget --------------------------------------------------------------
    def _expired(self) -> bool:
        return self._started is not None and (self._now() - self._started) >= self.policy.max_duration

    def _spend(self) -> None:
        self._actions += 1
        if self._actions > self.policy.max_total_actions:
            raise BrowserError(
                f"action budget exhausted ({self.policy.max_total_actions}); stopping rather than "
                "continuing to drive a page that keeps producing work"
            )

    # -- the cycle -----------------------------------------------------------
    def run(
        self,
        task: BrowserTask,
        *,
        values: dict[str, str],
        uploads: dict[str, str],
        declaration_authorised: bool = False,
        org_document_ids: Optional[set[str]] = None,
        submission_authorised: bool = False,
    ) -> RunReport:
        """Execute the task, or refuse to start.

        `validate_task` runs FIRST, before a browser is launched, so a misconfigured job costs
        nothing. It requires the set of document ids the organisation actually owns: a task naming
        another organisation's document is refused here rather than being discovered as a 403 on a
        funder's portal.

        `submission_authorised` is required separately from the task's own contents. A task that
        merely describes a portal the scope permits is not authority to submit - that is
        `submission_authority`'s decision, and this module refuses to infer it.
        """
        try:
            validate_task(task, org_document_ids=org_document_ids or set())
        except BrowserTaskRefused as exc:
            return RunReport(
                status="REJECTED",
                problems=[{"kind": "TASK_REFUSED", "detail": str(exc)}],
                outcome_certain=True,
            )

        report = RunReport(status="RUNNING")
        done: set[str] = set()
        declaration_accepted = False
        self._started = self._now()

        self.provider.launch(profile_dir=f"/tmp/granada-browser/{task.org_id}", headless=True)
        try:
            while True:
                if self._expired():
                    report.status = "FAILED"
                    report.problems.append(
                        {"kind": Failure.RESOURCE_LIMIT.value, "detail": "execution deadline reached"}
                    )
                    break

                page = self.provider.observe()
                self._checkpoint(OBSERVE, page, done, report)

                action = plan_next(
                    page,
                    values=values,
                    uploads=uploads,
                    already_done=done,
                    declaration_accepted=declaration_accepted,
                )

                if action.step in (Step.DONE, Step.BLOCKED, Step.FAILED):
                    report.status = {
                        Step.DONE: "COMPLETED",
                        Step.BLOCKED: "BLOCKED",
                    }.get(action.step, "FAILED")
                    if action.step == Step.BLOCKED:
                        report.problems.append({"kind": "BLOCKED", "detail": action.because})
                    break

                if action.step == Step.SUBMIT and not submission_authorised:
                    # The boundary's own rule, enforced here too: an unapproved task cannot submit
                    # even if the page offers the button. The scope permitting the host is NOT
                    # authority to submit - that comes from `submission_authority`.
                    report.status = "BLOCKED"
                    report.problems.append(
                        {
                            "kind": "SUBMISSION_NOT_AUTHORISED",
                            "detail": (
                                "the task does not carry submission authority; a permitted host is "
                                "not permission to submit"
                            ),
                        }
                    )
                    break

                if action.step == Step.DECLARE and not declaration_authorised:
                    report.status = "BLOCKED"
                    report.problems.append(
                        {
                            "kind": "DECLARATION_NOT_AUTHORISED",
                            "detail": "the form requires a legal declaration and this task carries no authority to make it",
                        }
                    )
                    break

                key = f"{action.step.value}:{action.target}"
                self._spend()
                result = self.provider.act(action.step, action.target, action.value)

                if not result.ok:
                    classified = result.failure or Failure.UNKNOWN
                    self._record_problem(report, action, classified, result.detail)

                    if action.step in CONSEQUENTIAL:
                        # THE rule. An ambiguous outcome on a consequential action is never retried
                        # here: it may have landed, and a retry would file a second application.
                        report.status = "UNCERTAIN"
                        report.outcome_certain = False
                        report.problems.append(
                            {
                                "kind": "UNCERTAIN_OUTCOME",
                                "detail": (
                                    f"{action.step.value} did not confirm; it may or may not have had "
                                    "an external effect, so it must be reconciled rather than retried"
                                ),
                            }
                        )
                        break

                    if classified not in RETRYABLE or not self._may_retry(key):
                        report.status = "FAILED" if classified != Failure.HUMAN_VERIFICATION else "BLOCKED"
                        break

                    self._attempts[key] = self._attempts.get(key, 0) + 1
                    report.recovery_attempts.append(
                        {
                            "step": action.step.value,
                            "target": action.target,
                            "failure": classified.value,
                            "attempt": self._attempts[key],
                        }
                    )
                    continue

                # SUCCESS: record and checkpoint.
                self._attempts.pop(key, None)
                if action.step == Step.DECLARE:
                    declaration_accepted = True
                if action.step == Step.UPLOAD:
                    report.uploaded.append(action.target)
                    done.add(action.target)
                elif action.step == Step.FILL:
                    report.field_outcomes[action.target] = "FILLED"
                    done.add(action.target)
                report.completed_steps.append(f"{action.step.value}:{action.target}")

                if action.step in CONSEQUENTIAL:
                    # Reached only on a reported success. Even here the status is not SUBMITTED
                    # without a receipt - the provider's own rule, and this module refuses to
                    # override it.
                    receipt = self._receipt_from(result)
                    if receipt:
                        report.receipt = receipt
                        report.status = "SUBMITTED"
                    else:
                        report.status = "UNCERTAIN"
                        report.outcome_certain = False

                self._checkpoint(VERIFY, page, done, report, consequential=action.step in CONSEQUENTIAL)

                if action.step in CONSEQUENTIAL:
                    break
        finally:
            # Release the session: the directive requires the resource be given back, and the
            # session belongs to the lease, not to this process.
            self.provider.close()

        if report.status == "RUNNING":
            report.status = "FAILED"
        return report

    # -- helpers -------------------------------------------------------------
    def _may_retry(self, key: str) -> bool:
        return self._attempts.get(key, 0) < self.policy.max_attempts_per_step

    def _record_problem(
        self, report: RunReport, action: PlannedAction, failure: Failure, detail: str
    ) -> None:
        report.problems.append(
            {
                "kind": failure.value,
                "step": action.step.value,
                "target": action.target,
                "detail": detail,
                # The diagnosis, so an operator can act without reproducing the run.
                "diagnosis": {
                    Failure.TRANSIENT_NETWORK: "the page did not load; retrying is safe",
                    Failure.ELEMENT_CHANGED: "the page structure moved; re-observe rather than re-run",
                    Failure.VALIDATION_REJECTED: "the receiving form refused the data it was given",
                    Failure.AUTH_REQUIRED: "the session is no longer authenticated",
                    Failure.HUMAN_VERIFICATION: (
                        "the site requires human verification; Granada does not circumvent it"
                    ),
                    Failure.ACCESS_DENIED: "the site refused access; this is an answer, not an obstacle",
                    Failure.OUT_OF_SCOPE: "the action was outside the permitted scope",
                    Failure.RESOURCE_LIMIT: "a bound was reached",
                    Failure.UNKNOWN: "the failure was not diagnosed, so it is not retried",
                }.get(failure, "undiagnosed"),
                "retryable": failure in RETRYABLE,
            }
        )

    def _receipt_from(self, result: ActionResult) -> Optional[str]:
        """A receipt must come from the provider's structured result, never from the presence of the
        word 'thank you' on a page."""
        if result.page and result.page.untrusted_text:
            # Explicitly NOT parsed for a receipt. Page text is untrusted input.
            pass
        return getattr(result, "receipt", None)

    def _checkpoint(
        self,
        cycle_step: str,
        page: Optional[PageState],
        done: set[str],
        report: RunReport,
        *,
        consequential: bool = False,
    ) -> None:
        prior = self._checkpoints[-1].consequential_attempted if self._checkpoints else False
        cp = Checkpoint(
            at=self._now(),
            cycle_step=cycle_step,
            page_url=page.url if page else "",
            completed=sorted(done),
            consequential_attempted=prior or consequential,
        )
        self._checkpoints.append(cp)
        report.checkpoints.append(cp.to_dict())

    # -- resume --------------------------------------------------------------
    def may_resume(self, checkpoints: list[dict[str, Any]]) -> tuple[bool, str]:
        """Whether a killed run may be resumed from its checkpoints.

        False once a consequential action has been attempted, because at that point the question is
        no longer "where were we" but "did it land" - which is reconciliation, not resumption.
        """
        if not checkpoints:
            return True, "no progress recorded; the run can start from the beginning"
        if any(c.get("consequential_attempted") for c in checkpoints):
            return False, (
                "a submission may already have been made; resume would risk filing a second "
                "application - reconcile the earlier attempt instead"
            )
        return True, "no consequential action was attempted, so resuming cannot duplicate a submission"
