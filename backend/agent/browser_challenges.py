"""Classifying what stopped a browser run, so it can be handled instead of merely failed.

THE DEFECT THIS REPLACES. Every obstacle was reported as BLOCKED, which is indistinguishable from a
genuine dead end. An email-verification step, a simple arithmetic question and a genuine image
CAPTCHA are three completely different problems with three different correct responses - continue,
answer, hand to a human - and collapsing them into one word made the flow unable to do the first two
and unable to explain the third.

Tiers come from `docs/verification-policy.md`:
  Tier 1  handled here, unattended.
  Tier 2  a bounded retry clears it.
  Tier 3  a human must act; the session is preserved so the run RESUMES rather than dies.

Pure functions over text: no browser, no database, so the boundaries are testable without a portal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class Challenge(str, Enum):
    NONE = "NONE"
    EMAIL_VERIFICATION = "EMAIL_VERIFICATION"
    EMAIL_CODE = "EMAIL_CODE"
    SMS_CODE = "SMS_CODE"
    SIMPLE_QUESTION = "SIMPLE_QUESTION"
    CHECKBOX = "CHECKBOX"
    IMAGE_CAPTCHA = "IMAGE_CAPTCHA"
    MFA_DEVICE = "MFA_DEVICE"
    IDENTITY_PROOF = "IDENTITY_PROOF"
    LEGAL_DECLARATION = "LEGAL_DECLARATION"
    PAYMENT = "PAYMENT"
    RATE_LIMIT = "RATE_LIMIT"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    #: A page that asks only to be acknowledged before showing the next step - "Yes, I understand",
    #: "you will be redirected". MEASURED on the UN Partner Portal: after the email step, Next reveals
    #: exactly this, with "Yes, I understand" to proceed and REGISTER beside it.
    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
    UNKNOWN_CHALLENGE = "UNKNOWN_CHALLENGE"


#: What to do about it. The action is the point of classifying at all.
class Disposition(str, Enum):
    CONTINUE = "CONTINUE"          # nothing to do; carry on
    HANDLE = "HANDLE"              # Granada can resolve this itself
    RETRY = "RETRY"                # bounded retry clears it
    HAND_IN = "HAND_IN"            # a human acts; the session must be preserved
    REFUSE = "REFUSE"              # never automated, by policy


@dataclass(frozen=True)
class Classification:
    challenge: Challenge
    disposition: Disposition
    tier: int
    reason: str
    #: For SIMPLE_QUESTION, the answer Granada worked out, when it could.
    answer: Optional[str] = None
    #: The text a human needs to see at a hand-in. Empty for non-human challenges.
    prompt: str = ""

    @property
    def resumable(self) -> bool:
        """A hand-in MUST be resumable. Losing the authenticated session is the defect that made
        earlier runs dead ends rather than pauses."""
        return self.disposition is Disposition.HAND_IN


# --- signatures -------------------------------------------------------------------------------
# Ordered: the first match wins, so the specific must precede the general. A page carrying both an
# email-verification notice and the word "captcha" in a script tag is an email verification.

#: An actual image/behavioural challenge. This is the one that exists to establish a human is
#: present, and it is refused rather than attempted.
_IMAGE_CHALLENGE = (
    r"select all (?:images|squares|photos)", r"select all that apply",
    r"click on all (?:images|squares)", r"solve (?:the|this) (?:challenge|puzzle)",
    r"identify (?:all )?(?:the )?(?:images|objects)",
)
#: A CAPTCHA WIDGET, which is not the same thing. A v2 checkbox frequently passes for a legitimate
#: browser with no interaction, and a v3 score is entirely non-interactive - so blanket-refusing on
#: the word "captcha" would refuse work Granada can legitimately do. Clicked once; if it then
#: presents an image grid, the next classification sees that text and becomes IMAGE_CAPTCHA.
_CAPTCHA_WIDGET = (
    r"g-recaptcha", r"grecaptcha", r"recaptcha/api\.js", r"h-captcha", r"hcaptcha\.com",
    r"cf-turnstile", r"i'?m not a robot", r"are you a robot",
    # The product name in prose, as portals phrase it ("Please complete the reCAPTCHA"). Tried after
    # the image-challenge patterns, so a grid of squares is still classified as the image challenge.
    r"recaptcha", r"hcaptcha",
)
_SMS_CODE = (r"we (?:sent|texted) (?:you )?(?:a|an) (?:code|sms)", r"enter the code (?:we )?sent to your (?:phone|mobile)",
             r"verification code sent to your (?:phone|mobile)", r"\d+-digit code.{0,40}(?:phone|mobile)",
             r"(?:text|sms) message with (?:a|your) code")
_EMAIL_CODE = (r"we (?:sent|emailed) (?:you )?(?:a|an) (?:code|link)", r"check your (?:email|inbox)",
               r"enter the code (?:we )?(?:sent|emailed) to", r"confirmation (?:code|link) (?:has been )?sent",
               # "a verification code to your email address" - the wording most portals actually use.
               r"(?:verification|confirmation|activation) (?:code|link)[^.]{0,60}(?:email|inbox|address)",
               r"(?:sent|emailed)[^.]{0,60}(?:email|inbox|address)",
               r"verify your email", r"activate your account", r"click the link in (?:the|your) email")
_SIMPLE_QUESTION = (r"what is \d+\s*[-+x*]\s*\d+", r"\d+\s*[-+x*]\s*\d+\s*=\s*\?",
                    r"how much is \d+ plus \d+", r"spell (?:the word )?[a-z]{3,}")
_MFA_DEVICE = (r"authenticator (?:app|code)", r"two-factor", r"2fa", r"security key", r"passkey")
_IDENTITY = (r"upload (?:a|your) (?:photo )?id", r"proof of identity", r"passport", r"selfie",
             r"liveness", r"verify your identity")
_LEGAL = (r"i (?:agree|accept) (?:to )?the (?:terms|conditions)", r"declaration", r"i certify",
          r"signature", r"sign here", r"authorised representative")
_PAYMENT = (r"application fee", r"pay(?:ment)? (?:of|now|by)", r"card number", r"billing address")
_RATE = (r"too many requests", r"try again (?:later|in \d+)", r"rate limit", r"temporarily unavailable")
_LOGIN = (r"sign in to continue", r"please log ?in", r"session (?:has )?expired", r"log in to your account")
#: A page whose only real content is "acknowledge this and continue". The wording is generic because
#: these interstitials are template copy, not site logic.
_ACKNOWLEDGE = (
    r"yes,? i understand", r"i understand", r"you will be redirected",
    r"please click on .{0,40}(?:sign up|register|continue|next)",
    r"got it", r"ok,? (?:continue|proceed)",
)


def _haystack(*parts: str) -> str:
    return "\n".join(p or "" for p in parts).lower()


def _matches(patterns: tuple[str, ...], text: str) -> Optional[str]:
    for pattern in patterns:
        found = re.search(pattern, text)
        if found:
            return found.group(0)
    return None


_ARITHMETIC = re.compile(r"(\d+)\s*([-+x*])\s*(\d+)")


def solve_simple_question(text: str) -> Optional[str]:
    """Answer an arithmetic challenge, or None.

    A sum like `7 + 3` is not a security boundary - it stops no bot worth the name - so treating it
    as one only blocks legitimate automation. It is solved, and the fact that it was solved by a
    simple expression evaluator (never `eval`) is recorded in the reason.

    Deliberately NOT attempted for a word the page asks to be spelled: that needs the page's own
    image or audio and is not a computation.
    """
    match = _ARITHMETIC.search(text or "")
    if not match:
        return None
    left, operator, right = int(match.group(1)), match.group(2), int(match.group(3))
    if operator == "+":
        return str(left + right)
    if operator == "-":
        return str(left - right)
    if operator in ("x", "*"):
        return str(left * right)
    return None


def classify(
    *,
    url: str = "",
    title: str = "",
    text: str = "",
    has_password_field: bool = False,
) -> Classification:
    """Identify the obstacle, and say what should happen about it."""
    hay = _haystack(title, text)
    where = f"{title!r} ({url})" if title or url else "the page"

    # --- Tier 3 first: these outrank everything, because acting on anything else is wrong ---
    if _matches(_IMAGE_CHALLENGE, hay):
        return Classification(
            Challenge.IMAGE_CAPTCHA, Disposition.REFUSE, 3,
            "an image/behavioural challenge is present; this exists to establish a human is present "
            "and Granada does not circumvent it",
            prompt="Complete the image challenge in the browser, then resume.",
        )
    if _matches(_MFA_DEVICE, hay):
        return Classification(
            Challenge.MFA_DEVICE, Disposition.HAND_IN, 3,
            "the portal requires a second factor held on a device Granada does not control",
            prompt="Enter the second-factor code, then resume.",
        )
    if _matches(_IDENTITY, hay):
        return Classification(
            Challenge.IDENTITY_PROOF, Disposition.HAND_IN, 3,
            "the portal requires proof of a person's identity",
            prompt="Provide the identity document the portal asks for, then resume.",
        )
    if _matches(_PAYMENT, hay):
        return Classification(
            Challenge.PAYMENT, Disposition.HAND_IN, 3,
            "the page asks for a payment; spending money needs a human's authority",
            prompt="Complete or authorise the payment, then resume.",
        )
    if _matches(_LEGAL, hay):
        return Classification(
            Challenge.LEGAL_DECLARATION, Disposition.HAND_IN, 3,
            "the page requires a legal declaration or signature, which creates liability for the entity",
            prompt="Review and accept the declaration, then resume.",
        )

    # --- Tier 1: handled unattended ---
    # ACKNOWLEDGEMENT BEFORE EVERYTHING ELSE IN TIER 1. The page says "you will be redirected ...
    # click on Sign up now", which would otherwise look like an email-verification or a navigation
    # step. Acknowledging PROCEDES; it commits nothing - the commitment is the control on the page it
    # leads to, which is still guarded.
    if _matches(_ACKNOWLEDGE, hay):
        return Classification(
            Challenge.ACKNOWLEDGEMENT, Disposition.HANDLE, 1,
            "the portal asks to be acknowledged before continuing; this proceeds without committing "
            "anything, so it is answered rather than treated as a declaration",
        )

    # Email BEFORE SMS: "check your email" frequently also mentions a phone fallback, and the email
    # path is the one Granada can complete.
    if _matches(_EMAIL_CODE, hay):
        return Classification(
            Challenge.EMAIL_VERIFICATION, Disposition.HANDLE, 1,
            "the portal has sent a verification message; Granada reads the organisation's own "
            "mailbox and completes it",
        )
    if _matches(_SMS_CODE, hay):
        return Classification(
            Challenge.SMS_CODE, Disposition.HAND_IN, 3,
            "the portal requires a code sent by SMS. Granada does not read a device's messages; the "
            "organisation can forward its SMS to its own mailbox, which turns this into the email path",
            prompt="Enter the SMS code, or set up forwarding so Granada can read it from the mailbox.",
        )

    if _matches(_RATE, hay):
        return Classification(
            Challenge.RATE_LIMIT, Disposition.RETRY, 2,
            "the portal is rate limiting; a bounded backoff clears this",
        )
    if _matches(_LOGIN, hay):
        return Classification(
            Challenge.LOGIN_REQUIRED, Disposition.HANDLE, 1,
            "the session is not authenticated; Granada re-uses the stored session for this "
            "organisation before falling back to credentials",
        )

    if _matches(_SIMPLE_QUESTION, hay) or _ARITHMETIC.search(hay):
        answer = solve_simple_question(hay)
        if answer is not None:
            return Classification(
                Challenge.SIMPLE_QUESTION, Disposition.HANDLE, 1,
                "a simple arithmetic challenge; this is not meaningful bot protection and is "
                "answered rather than treated as a boundary",
                answer=answer,
            )

    if _matches(_CAPTCHA_WIDGET, hay):
        # A widget is not an image challenge. Clicked once; whether it passes is then observable, and
        # an image grid would be classified as IMAGE_CAPTCHA on the following turn.
        return Classification(
            Challenge.CHECKBOX, Disposition.HANDLE, 1,
            "a CAPTCHA widget is present; a checkbox usually passes for a legitimate browser, so it "
            "is clicked once and the result observed rather than assumed either way",
        )

    if re.search(r"captcha", hay):
        # The word appears with nothing identifiable. Do NOT guess that it is passable, and do not
        # guess that it needs a human either - that is an UNKNOWN, and saying so is the honest answer.
        return Classification(
            Challenge.UNKNOWN_CHALLENGE, Disposition.HAND_IN, 3,
            "the page mentions a challenge that could not be identified; refusing to guess whether "
            "it is passable, and handing it to a human rather than failing the run",
            prompt="Inspect the challenge in the browser and complete it, then resume.",
        )

    if has_password_field and "sign in" in hay:
        return Classification(
            Challenge.LOGIN_REQUIRED, Disposition.HANDLE, 1,
            "a sign-in form is present and the stored session was not authenticated",
        )

    return Classification(Challenge.NONE, Disposition.CONTINUE, 1, f"no challenge detected on {where}")


def describe() -> dict[str, Any]:
    return {
        "tiers": {1: "handled unattended", 2: "bounded retry", 3: "human, session preserved"},
        "challenges": [c.value for c in Challenge],
        "policy": "docs/verification-policy.md",
    }
