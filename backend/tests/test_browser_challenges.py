"""Challenge classification: the right response to each obstacle, not one word for all of them.

The behaviour that matters is the DISPOSITION. An email-verification step must be handled, a simple
sum answered, a rate limit retried, and an image CAPTCHA handed to a human with the session kept
alive. Reporting all five as BLOCKED made the flow unable to do the first three and unable to explain
the last two.
"""

import pytest

from agent.browser_challenges import (
    Challenge,
    Disposition,
    classify,
    solve_simple_question,
)


# ---------------------------------------------------------------------------
# Tier 1 - unattended
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "We have sent a verification code to your email address.",
        "Check your inbox to confirm your account.",
        "Please verify your email to continue.",
        "Click the link in your email to activate your account.",
    ],
)
def test_email_verification_is_handled_not_failed(text):
    out = classify(text=text)
    assert out.challenge is Challenge.EMAIL_VERIFICATION
    assert out.disposition is Disposition.HANDLE
    assert out.tier == 1


def test_email_is_preferred_over_sms_when_both_are_offered():
    """'Check your email' pages routinely mention a phone fallback. The email path is the one
    Granada can actually complete, so it must win."""
    out = classify(text="Check your email for the code, or enter the code sent to your phone.")
    assert out.challenge is Challenge.EMAIL_VERIFICATION
    assert out.disposition is Disposition.HANDLE


def test_a_simple_sum_is_answered():
    out = classify(text="Security check: what is 7 + 3 = ?")
    assert out.challenge is Challenge.SIMPLE_QUESTION
    assert out.disposition is Disposition.HANDLE
    assert out.answer == "10"


def test_an_expired_session_is_re_established_from_the_stored_one():
    out = classify(text="Your session has expired. Please log in to your account.")
    assert out.challenge is Challenge.LOGIN_REQUIRED
    assert out.disposition is Disposition.HANDLE


# ---------------------------------------------------------------------------
# Tier 2 - bounded retry
# ---------------------------------------------------------------------------
def test_rate_limiting_is_retried_not_failed():
    out = classify(text="Too many requests. Try again in 60 seconds.")
    assert out.challenge is Challenge.RATE_LIMIT
    assert out.disposition is Disposition.RETRY
    assert out.tier == 2


# ---------------------------------------------------------------------------
# Tier 3 - human, and the session must survive
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,challenge",
    [
        ("Select all squares with traffic lights", Challenge.IMAGE_CAPTCHA),
        ("Please solve the puzzle to continue", Challenge.IMAGE_CAPTCHA),
        ("Enter the code from your authenticator app.", Challenge.MFA_DEVICE),
        ("Upload a copy of your passport to verify your identity.", Challenge.IDENTITY_PROOF),
        ("I agree to the terms and conditions", Challenge.LEGAL_DECLARATION),
        ("An application fee of $50 is required.", Challenge.PAYMENT),
    ],
)
def test_human_gates_are_handed_in_with_a_prompt(text, challenge):
    out = classify(text=text)
    assert out.challenge is challenge
    assert out.disposition in (Disposition.HAND_IN, Disposition.REFUSE)
    assert out.tier == 3
    assert out.prompt, "a hand-in without a prompt tells the operator nothing to do"


def test_an_image_challenge_is_refused_by_policy_not_merely_deferred():
    out = classify(text="Select all images with bicycles")
    assert out.disposition is Disposition.REFUSE


def test_a_captcha_widget_is_clicked_rather_than_refused():
    """A v2 checkbox often passes for a legitimate browser and a v3 score is non-interactive.
    Refusing on the word 'captcha' would refuse work Granada can legitimately do."""
    out = classify(text="Please complete the reCAPTCHA to continue.")
    assert out.challenge is Challenge.CHECKBOX
    assert out.disposition is Disposition.HANDLE
    assert out.tier == 1


def test_every_hand_in_is_resumable():
    """Losing the authenticated session is what made earlier runs dead ends rather than pauses."""
    out = classify(text="Enter the code from your authenticator app.")
    assert out.resumable


def test_an_sms_code_points_at_the_forwarding_route():
    out = classify(text="Enter the 6-digit code sent to your mobile phone.")
    assert out.challenge in (Challenge.SMS_CODE, Challenge.EMAIL_VERIFICATION)
    if out.challenge is Challenge.SMS_CODE:
        assert "forward" in out.reason


# ---------------------------------------------------------------------------
# Unidentified challenges - neither assumed passable nor assumed human
# ---------------------------------------------------------------------------
def test_an_unidentified_challenge_is_not_assumed_passable():
    """The word 'captcha' with no recognisable form must not be treated as handled."""
    out = classify(text="Please complete the captcha.")
    assert out.challenge in (Challenge.IMAGE_CAPTCHA, Challenge.UNKNOWN_CHALLENGE)
    assert out.disposition in (Disposition.REFUSE, Disposition.HAND_IN)


def test_a_clean_page_continues():
    out = classify(title="Registration", text="Please provide your organisation details.")
    assert out.challenge is Challenge.NONE
    assert out.disposition is Disposition.CONTINUE


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("what is 7 + 3", "10"),
        ("12 - 5 = ?", "7"),
        ("4 x 6", "24"),
        ("9 * 9", "81"),
        ("no sum here", None),
    ],
)
def test_arithmetic_is_evaluated_without_eval(text, expected):
    assert solve_simple_question(text) == expected


def test_a_spelling_challenge_is_not_faked():
    """The page asks for a word from its own image; guessing would be inventing an answer."""
    out = classify(text="Please spell the word shown")
    assert out.answer is None
