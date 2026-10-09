"""What a control actually does, decided from context rather than from its label.

THE PROBLEM, AS THE DIRECTIVE STATES IT
---------------------------------------
§1: "Do not assume that similarly labelled buttons perform identical operations. The execution engine
must evaluate context before deciding."

A button reading *Continue* may advance to the next page, save a draft, confirm a declaration, submit
the final application, or leave the site entirely. All five are the same word. The difference between
them is the difference between a saved draft and a filed application - and in a system that submits
grant applications on an organisation's behalf, that is not a nuance.

So intent is DERIVED from the page: what the form contains, whether validation has passed, where the
progress indicator sits, what the URL says, and what the surrounding text asks. A label alone is not
evidence.

WHY THIS IS NOT A MODEL CALL
----------------------------
Intent resolution here is deterministic, and deliberately so. §3 says not to spend an expensive
inference on work that deterministic verification settles, and §12 says "validate every proposed
action against the actual current browser state". A rule that can be read, tested and explained is
better than a probability for a decision with legal consequences.

A model MAY propose an intent when the page is genuinely ambiguous. When it does, the proposal is
checked against the same evidence this module uses, and `uncertain` is the default when they disagree.
The model proposes; the deterministic layer decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from .multimodal_routing import ImageRef, Observation


class Intent(str, Enum):
    """What an action is FOR. The consequence, not the label."""

    ADVANCE = "ADVANCE"                # move to the next section; nothing external happens
    SAVE = "SAVE"                      # persist a draft; nothing external happens
    DECLARE = "DECLARE"                # make a legal statement on the organisation's behalf
    SUBMIT = "SUBMIT"                  # hand the application to the funder
    LEAVE_SITE = "LEAVE_SITE"          # navigate away from the application
    CANCEL = "CANCEL"                  # abandon, possibly destructively
    AUTHENTICATE = "AUTHENTICATE"      # sign in or re-authenticate
    HUMAN_VERIFICATION = "HUMAN_VERIFICATION"   # a challenge Granada must not circumvent
    UNKNOWN = "UNKNOWN"                # could not be determined from the evidence


#: Intents that may have an effect outside Granada. Never retried on an ambiguous outcome, and the
#: reason `submission_lifecycle` has an UNCERTAIN state.
CONSEQUENTIAL_INTENTS: frozenset[Intent] = frozenset({Intent.SUBMIT, Intent.DECLARE, Intent.CANCEL})

#: Intents that require explicit authority before they may be attempted.
REQUIRES_AUTHORITY: frozenset[Intent] = frozenset({Intent.SUBMIT, Intent.DECLARE})


@dataclass(frozen=True)
class PageContext:
    """Everything needed to decide what a control does.

    Carries what §1 lists as the things to understand before acting: the page, its stage, what is
    complete, and what is still missing.
    """

    url: str
    title: str = ""
    #: Visible controls by their text.
    controls: list[str] = field(default_factory=list)
    #: A progress indicator's current stage, when the page renders one.
    stage: Optional[int] = None
    total_stages: Optional[int] = None
    #: Fields the page still reports as unsatisfied.
    validation_messages: list[str] = field(default_factory=list)
    #: Whether every field the page declared required now holds a value Granada supplied.
    required_satisfied: bool = True
    #: Text around the control, which is often where "you cannot submit yet" is said.
    surrounding_text: str = ""
    #: Whether the page offers a file input that has no file attached.
    awaiting_upload: bool = False
    #: True when the page has said the submission already happened.
    confirmation_visible: bool = False
    #: A login form is present.
    login_present: bool = False
    #: A CAPTCHA or equivalent challenge is present.
    challenge_present: bool = False
    #: Fields whose values Granada supplied, for checking a control's claim against reality.
    fields_filled: int = 0


@dataclass(frozen=True)
class GroundedAction:
    """A control, what it is believed to do, and the evidence for that belief.

    `expected_result` is the observable outcome to verify afterwards. §4 step 6 is explicit: "Do not
    treat a successful mouse click or browser command as proof of task completion." Naming the
    expectation BEFORE acting is what makes the verification meaningful rather than a restatement.
    """

    control: str
    intent: Intent
    expected_result: str
    evidence: list[str] = field(default_factory=list)
    #: False when the evidence conflicts or is thin. A caller must not act on an ungrounded action.
    grounded: bool = True
    #: Set when a model proposed this and the deterministic evidence did not corroborate it.
    model_disagreement: bool = False

    @property
    def is_consequential(self) -> bool:
        return self.intent in CONSEQUENTIAL_INTENTS


class UngroundedAction(RuntimeError):
    """The control's effect could not be established from the evidence."""


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def ground(context: PageContext, control: str, *, model_proposal: Optional[Intent] = None) -> GroundedAction:
    """Determine what `control` does in THIS page context.

    Ordered by evidence strength, most decisive first. A page stating that a submission already
    happened outranks the word on any button: if a confirmation is visible, no control can be a
    first submission, whatever it says.
    """
    label = _normalise(control)
    evidence: list[str] = []

    # -- 1. Already submitted. Outranks every label. -------------------------
    if context.confirmation_visible:
        return GroundedAction(
            control=control,
            intent=Intent.LEAVE_SITE,
            expected_result="the page changes away from the confirmation",
            evidence=["a submission confirmation is already visible, so no control here can be a first submission"],
        )

    # -- 2. Challenges and authentication stop the analysis. -----------------
    if context.challenge_present:
        return GroundedAction(
            control=control,
            intent=Intent.HUMAN_VERIFICATION,
            expected_result="none; this requires a human",
            evidence=["a verification challenge is present; Granada does not circumvent it"],
        )
    if context.login_present or any(w in label for w in ("sign in", "log in", "login")):
        return GroundedAction(
            control=control,
            intent=Intent.AUTHENTICATE,
            expected_result="an authenticated session",
            evidence=["a login form is present"],
        )

    # -- 3. Explicit destructive or exit wording. ----------------------------
    if any(w in label for w in ("cancel", "withdraw", "delete", "discard")):
        return GroundedAction(
            control=control,
            intent=Intent.CANCEL,
            expected_result="the application is abandoned",
            evidence=[f"the label {control!r} names a destructive action"],
        )
    if any(w in label for w in ("back to", "return to site", "home", "exit", "close")):
        return GroundedAction(
            control=control,
            intent=Intent.LEAVE_SITE,
            expected_result="navigation away from the application",
            evidence=[f"the label {control!r} names navigation away"],
        )

    # -- 4. Explicit submission wording. -------------------------------------
    if any(w in label for w in ("submit", "send application", "file application", "apply now")):
        # A submit control is only a real submission when the page is ready for one. A "Submit" that
        # the page has disabled behind validation is an ADVANCE-shaped no-op, and treating it as a
        # submission would make Granada report a filing that never happened.
        if not context.required_satisfied or context.validation_messages or context.awaiting_upload:
            return GroundedAction(
                control=control,
                intent=Intent.ADVANCE,
                expected_result="validation is re-presented rather than a submission occurring",
                evidence=[
                    f"the label {control!r} says submit, but the page is not ready",
                    *(["validation messages are present"] if context.validation_messages else []),
                    *(["a required upload is missing"] if context.awaiting_upload else []),
                ],
                grounded=True,
            )
        return GroundedAction(
            control=control,
            intent=Intent.SUBMIT,
            expected_result="a confirmation page with a submission reference",
            evidence=[
                f"the label {control!r} names submission",
                "no validation messages are present",
                "every required field is satisfied",
            ],
        )

    # -- 5. Declarations. A legal statement, not a navigation. ---------------
    if any(w in label for w in ("declare", "certify", "i confirm", "i agree", "attest")):
        return GroundedAction(
            control=control,
            intent=Intent.DECLARE,
            expected_result="the declaration is recorded and the form may proceed",
            evidence=[f"the label {control!r} makes a legal statement on the organisation's behalf"],
        )

    # -- 6. Save. ------------------------------------------------------------
    if any(w in label for w in ("save", "save draft", "store", "keep")):
        return GroundedAction(
            control=control,
            intent=Intent.SAVE,
            expected_result="the draft is persisted and the page stays put",
            evidence=[f"the label {control!r} names persistence without progression"],
        )

    # -- 7. The ambiguous one: Continue, Next, Proceed. ----------------------
    # THE case the directive names. Decided by the page, never by the word.
    if any(w in label for w in ("continue", "next", "proceed", "go on")):
        blockers = []
        if not context.required_satisfied:
            blockers.append("required fields are not satisfied")
        if context.validation_messages:
            blockers.append(f"the page reports: {'; '.join(context.validation_messages[:2])}")
        if context.awaiting_upload:
            blockers.append("a required upload is missing")

        if blockers:
            # Not progress: the page will re-present itself. Naming it ADVANCE would make a stalled
            # form look like forward motion.
            return GroundedAction(
                control=control,
                intent=Intent.ADVANCE,
                expected_result="the same section is re-presented with validation shown",
                evidence=[f"the label {control!r} suggests progress, but the page is not ready", *blockers],
            )

        if context.stage is not None and context.total_stages is not None:
            if context.stage >= context.total_stages:
                # The last section. A "Continue" here is the submission in all but name, and treating
                # it as navigation is how an application gets filed without authority.
                return GroundedAction(
                    control=control,
                    intent=Intent.SUBMIT,
                    expected_result="a confirmation page with a submission reference",
                    evidence=[
                        f"the label {control!r} is ambiguous on its own",
                        f"but the progress indicator reads stage {context.stage} of {context.total_stages}",
                        "the final section's forward control is the submission",
                    ],
                )
            return GroundedAction(
                control=control,
                intent=Intent.ADVANCE,
                expected_result=f"stage {context.stage + 1} of {context.total_stages} is shown",
                evidence=[
                    f"the progress indicator reads stage {context.stage} of {context.total_stages}",
                    "no validation messages are present",
                ],
            )

        # No progress indicator: fall back to the surrounding text, then decline.
        low_surround = _normalise(context.surrounding_text)
        if any(w in low_surround for w in ("final", "last step", "ready to submit", "complete your application")):
            return GroundedAction(
                control=control,
                intent=Intent.SUBMIT,
                expected_result="a confirmation page with a submission reference",
                evidence=[
                    f"the label {control!r} is ambiguous on its own",
                    "the surrounding text identifies this as the final step",
                ],
            )

        if model_proposal is not None:
            return _from_model(context, control, model_proposal, evidence)

        return GroundedAction(
            control=control,
            intent=Intent.UNKNOWN,
            expected_result="none; the effect of this control was not established",
            evidence=[
                f"the label {control!r} is ambiguous",
                "there is no progress indicator and no surrounding text to disambiguate it",
                "§1 forbids assuming that similarly labelled buttons do the same thing",
            ],
            grounded=False,
        )

    # -- 8. Anything else. ---------------------------------------------------
    if model_proposal is not None:
        return _from_model(context, control, model_proposal, evidence)

    return GroundedAction(
        control=control,
        intent=Intent.UNKNOWN,
        expected_result="none; the effect of this control was not established",
        evidence=[f"no evidence establishes what {control!r} does"],
        grounded=False,
    )


def _from_model(
    context: PageContext, control: str, proposal: Intent, prior_evidence: list[str]
) -> GroundedAction:
    """Accept a model's proposed intent ONLY when nothing in the evidence contradicts it.

    §12: the model proposes, and every proposed action is validated against the actual current browser
    state. A proposal that would make a control consequential while the page shows validation failures
    is refused - the deterministic layer decides, and disagreement is recorded rather than resolved in
    the model's favour.
    """
    contradicts = (
        proposal in CONSEQUENTIAL_INTENTS
        and (not context.required_satisfied or bool(context.validation_messages) or context.awaiting_upload)
    )
    if contradicts:
        return GroundedAction(
            control=control,
            intent=Intent.ADVANCE,
            expected_result="the same section is re-presented with validation shown",
            evidence=[
                f"a model proposed {proposal.value}, but the page is not ready",
                *prior_evidence,
            ],
            model_disagreement=True,
        )
    return GroundedAction(
        control=control,
        intent=proposal,
        expected_result="as proposed by the model; verify against the page afterwards",
        evidence=[f"a model proposed {proposal.value}", *prior_evidence],
    )


def must_gather_more_evidence(action: GroundedAction) -> bool:
    """Whether acting now would be acting on an assumption.

    §7: "Where visual identification is uncertain, gather additional evidence before acting."
    """
    return not action.grounded or action.intent is Intent.UNKNOWN


def require_authority(action: GroundedAction, *, submission_authorised: bool, declaration_authorised: bool) -> None:
    """Refuse a consequential action that carries no authority.

    A separate check from grounding: knowing what a button does is not permission to press it, and
    §10 requires the model to propose while a deterministic policy decides.
    """
    if action.intent is Intent.SUBMIT and not submission_authorised:
        raise UngroundedAction(
            f"{action.control!r} would submit and this task carries no submission authority; "
            "a permitted host is not permission to submit"
        )
    if action.intent is Intent.DECLARE and not declaration_authorised:
        raise UngroundedAction(
            f"{action.control!r} would make a legal declaration and this task carries no authority "
            "to make one"
        )


def describe() -> dict[str, Any]:
    """The rules, stated where a reviewer will find them."""
    return {
        "intents": sorted(i.value for i in Intent),
        "consequential": sorted(i.value for i in CONSEQUENTIAL_INTENTS),
        "requires_authority": sorted(i.value for i in REQUIRES_AUTHORITY),
        "ambiguity_rule": (
            "an ambiguous label is resolved from the page - progress indicator, validation state, "
            "surrounding text - and reported UNKNOWN rather than guessed when none of that settles it"
        ),
        "last_stage_rule": (
            "a Continue on the final section is treated as SUBMIT, because treating it as navigation "
            "is how an application gets filed without authority"
        ),
        "model_rule": (
            "a model may propose an intent; a proposal that would make a control consequential while "
            "the page is not ready is refused and the disagreement is recorded"
        ),
        "does_not_do": [
            "it does not perform actions - browser_runtime does",
            "it does not decide authority - submission_authority does",
        ],
    }
