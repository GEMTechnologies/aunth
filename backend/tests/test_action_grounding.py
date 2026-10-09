"""Action grounding. The directive's case is a button labelled "Continue", and what it does.

§1: "Do not assume that similarly labelled buttons perform identical operations."

All five meanings of Continue - advance, save, declare, submit, leave - are the same word. The
difference between them is the difference between a saved draft and a filed application.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.action_grounding import (  # noqa: E402
    CONSEQUENTIAL_INTENTS,
    REQUIRES_AUTHORITY,
    GroundedAction,
    Intent,
    PageContext,
    UngroundedAction,
    describe,
    ground,
    must_gather_more_evidence,
    require_authority,
)


def ctx(**over) -> PageContext:
    base = dict(url="https://portal.example/apply")
    base.update(over)
    return PageContext(**base)  # type: ignore[arg-type]


# ===========================================================================
# THE DIRECTIVE'S CASE: "Continue" IS FIVE DIFFERENT THINGS
# ===========================================================================
def test_continue_on_a_middle_stage_advances():
    a = ground(ctx(stage=1, total_stages=3, controls=["Continue"]), "Continue")
    assert a.intent is Intent.ADVANCE
    assert "stage 2 of 3" in a.expected_result


def test_continue_on_the_FINAL_stage_is_a_submission():
    """THE case the directive is about. Treating a final "Continue" as navigation is how an
    application gets filed without authority."""
    a = ground(ctx(stage=3, total_stages=3, controls=["Continue"]), "Continue")
    assert a.intent is Intent.SUBMIT
    assert "reference" in a.expected_result


def test_continue_while_the_page_is_not_ready_re_presents_rather_than_progresses():
    a = ground(
        ctx(stage=1, total_stages=3, required_satisfied=False, controls=["Continue"]),
        "Continue",
    )
    assert a.intent is Intent.ADVANCE
    assert "re-presented" in a.expected_result, "a stalled form must not look like forward motion"


def test_continue_with_validation_messages_is_not_a_submission():
    a = ground(
        ctx(stage=3, total_stages=3, validation_messages=["Amount exceeds the ceiling"], controls=["Continue"]),
        "Continue",
    )
    assert a.intent is not Intent.SUBMIT


def test_surrounding_text_can_identify_a_final_step_with_no_progress_indicator():
    a = ground(
        ctx(controls=["Continue"], surrounding_text="This is the final step. Review before you submit."),
        "Continue",
    )
    assert a.intent is Intent.SUBMIT


def test_an_undecidable_continue_is_UNKNOWN_rather_than_a_guess():
    """With no progress indicator and no surrounding text, the honest answer is that Granada does not
    know - and §7 says gather more evidence before acting."""
    a = ground(ctx(controls=["Continue"]), "Continue")
    assert a.intent is Intent.UNKNOWN
    assert a.grounded is False
    assert must_gather_more_evidence(a) is True
    assert "similarly labelled buttons" in " ".join(a.evidence)


# ===========================================================================
# A CONFIRMATION OUTRANKS ANY LABEL
# ===========================================================================
def test_a_visible_confirmation_means_no_control_here_is_a_first_submission():
    a = ground(ctx(confirmation_visible=True, controls=["Submit application"]), "Submit application")
    assert a.intent is Intent.LEAVE_SITE, "a submission cannot happen twice on a confirmation page"


# ===========================================================================
# SUBMIT IS ONLY SUBMIT WHEN THE PAGE IS READY
# ===========================================================================
def test_a_submit_button_on_an_unready_page_is_not_a_submission():
    """A "Submit" disabled behind validation is not a filing. Reporting it as one would claim an
    application that never left Granada."""
    a = ground(
        ctx(controls=["Submit application"], required_satisfied=False),
        "Submit application",
    )
    assert a.intent is Intent.ADVANCE
    assert a.is_consequential is False


def test_a_submit_button_on_a_missing_upload_is_not_a_submission():
    a = ground(ctx(controls=["Submit application"], awaiting_upload=True), "Submit application")
    assert a.intent is Intent.ADVANCE


def test_a_ready_submit_button_IS_a_submission():
    a = ground(ctx(controls=["Submit application"], required_satisfied=True), "Submit application")
    assert a.intent is Intent.SUBMIT
    assert a.is_consequential is True


# ===========================================================================
# DECLARATIONS ARE LEGAL, NOT NAVIGATION
# ===========================================================================
def test_a_declaration_is_recognised_as_declare_not_advance():
    a = ground(ctx(controls=["I declare that the information given is accurate"]), "I declare that the information given is accurate")
    assert a.intent is Intent.DECLARE
    assert a.is_consequential is True


def test_certify_and_attest_are_declarations_too():
    for label in ("I certify this is true", "I attest to the accuracy", "I confirm the above"):
        assert ground(ctx(), label).intent is Intent.DECLARE, label


# ===========================================================================
# SAVE, CANCEL, LEAVE, AUTH, CHALLENGE
# ===========================================================================
def test_save_is_not_progress():
    a = ground(ctx(controls=["Save draft"]), "Save draft")
    assert a.intent is Intent.SAVE
    assert a.is_consequential is False


def test_cancel_is_destructive_and_consequential():
    a = ground(ctx(controls=["Cancel application"]), "Cancel application")
    assert a.intent is Intent.CANCEL
    assert a.is_consequential is True


def test_navigation_away_is_recognised():
    assert ground(ctx(), "Back to site").intent is Intent.LEAVE_SITE


def test_a_login_form_makes_the_control_authentication():
    a = ground(ctx(login_present=True, controls=["Continue"]), "Continue")
    assert a.intent is Intent.AUTHENTICATE
    assert "authenticated session" in a.expected_result


def test_a_challenge_blocks_rather_than_being_solved():
    """§9: do not build uncontrolled CAPTCHA circumvention, and do not assume a local browser is
    undetectable."""
    a = ground(ctx(challenge_present=True, controls=["Continue"]), "Continue")
    assert a.intent is Intent.HUMAN_VERIFICATION
    assert "does not circumvent" in " ".join(a.evidence)


# ===========================================================================
# THE MODEL PROPOSES, THE DETERMINISTIC LAYER DECIDES
# ===========================================================================
def test_a_model_proposal_is_accepted_when_nothing_contradicts_it():
    a = ground(ctx(controls=["Weiter"]), "Weiter", model_proposal=Intent.ADVANCE)
    assert a.intent is Intent.ADVANCE
    assert a.grounded is True


def test_a_model_proposal_that_would_submit_an_unready_form_is_REFUSED():
    """§12: validate every proposed action against the actual current browser state. A model saying
    "this submits" does not make it true when the page shows validation failures."""
    a = ground(
        ctx(controls=["Weiter"], validation_messages=["Required field missing"]),
        "Weiter",
        model_proposal=Intent.SUBMIT,
    )
    assert a.intent is Intent.ADVANCE
    assert a.model_disagreement is True
    assert "not ready" in " ".join(a.evidence)


def test_a_model_proposing_a_declaration_on_an_unready_page_is_also_refused():
    a = ground(
        ctx(controls=["Weiter"], awaiting_upload=True),
        "Weiter",
        model_proposal=Intent.DECLARE,
    )
    assert a.intent is Intent.ADVANCE
    assert a.model_disagreement is True


def test_an_unknown_control_with_no_proposal_stays_unknown():
    a = ground(ctx(), "⟳")
    assert a.intent is Intent.UNKNOWN
    assert a.grounded is False


# ===========================================================================
# AUTHORITY IS SEPARATE FROM GROUNDING
# ===========================================================================
def test_grounding_a_submit_does_not_authorise_it():
    """Knowing what a button does is not permission to press it."""
    a = ground(ctx(controls=["Submit application"]), "Submit application")
    assert a.intent is Intent.SUBMIT
    with pytest.raises(UngroundedAction) as e:
        require_authority(a, submission_authorised=False, declaration_authorised=False)
    assert "permitted host is not permission" in str(e.value)


def test_an_authorised_submission_passes_the_check():
    a = ground(ctx(controls=["Submit application"]), "Submit application")
    require_authority(a, submission_authorised=True, declaration_authorised=False)


def test_an_unauthorised_declaration_is_refused():
    a = ground(ctx(), "I declare that the information is accurate")
    with pytest.raises(UngroundedAction):
        require_authority(a, submission_authorised=True, declaration_authorised=False)


def test_an_ordinary_advance_needs_no_authority():
    a = ground(ctx(stage=1, total_stages=3), "Continue")
    require_authority(a, submission_authorised=False, declaration_authorised=False)


# ===========================================================================
# EXPECTED RESULT IS NAMED BEFORE ACTING
# ===========================================================================
def test_every_grounded_action_names_an_expected_result():
    """§4 step 6: a successful click is not proof of completion. Naming the expectation BEFORE
    acting is what makes the verification meaningful rather than a restatement."""
    for label, c in [
        ("Continue", ctx(stage=1, total_stages=3)),
        ("Submit application", ctx()),
        ("Save draft", ctx()),
        ("I declare this is true", ctx()),
        ("Back to site", ctx()),
    ]:
        a = ground(c, label)
        assert a.expected_result, f"{label} has no expected result"
        assert a.evidence, f"{label} has no evidence"


# ===========================================================================
# THE BOUNDARY IS STATED
# ===========================================================================
def test_describe_states_the_ambiguity_and_last_stage_rules():
    d = describe()
    assert "UNKNOWN rather than guessed" in d["ambiguity_rule"]
    assert "filed without authority" in d["last_stage_rule"]
    assert set(d["consequential"]) == {"SUBMIT", "DECLARE", "CANCEL"}
    joined = " ".join(d["does_not_do"])
    assert "browser_runtime" in joined
    assert "submission_authority" in joined


def test_consequential_intents_are_a_short_explicit_list():
    """If this grows, the retry and authority rules silently weaken."""
    assert CONSEQUENTIAL_INTENTS == frozenset({Intent.SUBMIT, Intent.DECLARE, Intent.CANCEL})
    assert REQUIRES_AUTHORITY == frozenset({Intent.SUBMIT, Intent.DECLARE})
