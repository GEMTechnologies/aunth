"""Recognising a portal's verification message, and taking the code or link out of it.

WHY THIS IS NOT A REGEX AND A HOPE. The message that completes a registration is the one thing an
unattended flow cannot get wrong: a code applied to the wrong account, or a link from an unrelated
message, is worse than stopping. So an extraction only counts when it is tied to a request Granada
actually made - the same organisation, the same mailbox, after the request, before it expired.

Everything here is pure: no database, no clock of its own. `received_at` and the request's window are
supplied, so the behaviour is testable at the boundaries (an old code, a future-dated message, an
expired request) without waiting for real mail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Optional


class VerificationKind(str, Enum):
    CODE = "CODE"
    LINK = "LINK"
    NONE = "NONE"


#: The shortest and longest a numeric code plausibly is. Below 4 is a year or a quantity; above 8 is
#: an order number. Both appear in the same emails as the real code, which is why a bare digit-run
#: search is not enough on its own.
CODE_MIN, CODE_MAX = 4, 8

#: Words that make a message a verification message rather than an acknowledgement. Deliberately
#: broad on the SUBJECT, because a portal that says only "Verify your email" still has to be
#: recognised.
_VERIFY_TERMS = (
    "verify", "verification", "confirm", "confirmation", "activate", "activation",
    "validate", "one-time", "one time", "otp", "passcode", "security code", "your code",
)

_CODE_PATTERNS = (
    # "code 4839 2017" - some templates space the digits in two groups. TRIED FIRST: the generic
    # numeric pattern below would otherwise match the first group alone and return a truncated code.
    re.compile(r"(?:code|passcode|otp|pin)\D{0,20}?(\d{4})[\s-](\d{2,4})", re.IGNORECASE),
    # "your code is 123456", "verification code: 483920"
    re.compile(r"(?:code|passcode|otp|pin)\D{0,20}?(\d{4,8})", re.IGNORECASE),
    # A code shown alone on its own line, which is how most templates render it.
    re.compile(r"^\s*(\d{4,8})\s*$", re.MULTILINE),
    # Alphanumeric code, e.g. "code: A1B2C3".
    re.compile(r"(?:code|passcode|otp)\W{0,20}([A-Z0-9]{5,10})\b", re.IGNORECASE),
)

_LINK_PATTERN = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)

#: Paths/substrings that mark a link as the verification link rather than a footer link.
_LINK_MARKERS = (
    "verify", "verif", "confirm", "activation", "activate", "validate",
    "token=", "code=", "otp=", "auth", "signup/confirm", "registration",
)

#: Links that are never the one we want, however tempting the match. A footer "unsubscribe" or a
#: tracking pixel host would otherwise be picked as the confirmation URL.
_LINK_DENY = ("unsubscribe", "privacy", "terms", "tracking", "pixel", "open.aspx", "beacon")


@dataclass(frozen=True)
class VerificationRequest:
    """A verification Granada is WAITING for, created when it triggered one.

    This is the anchor that makes extraction safe. Without it, any message containing a six-digit
    number would satisfy any waiting workflow - which is how a code for one organisation's account
    gets typed into another portal.
    """

    org_id: str
    address: str
    portal_host: str
    purpose: str
    requested_at: datetime
    ttl: timedelta = timedelta(minutes=30)
    #: Every mailbox the code is legitimately allowed to arrive at. Usually one, but an organisation
    #: may nominate an alias.
    also_accept: tuple[str, ...] = ()

    @property
    def expires_at(self) -> datetime:
        return self.requested_at + self.ttl

    def accepts_address(self, candidate: str) -> bool:
        wanted = {self.address.strip().lower(), *(a.strip().lower() for a in self.also_accept)}
        return candidate.strip().lower() in wanted

    def within_window(self, received_at: datetime) -> bool:
        """After the request and before expiry.

        A message that arrived BEFORE the request cannot be its verification - that is a previous
        attempt's code, and using it would bind the wrong account. Clock skew is tolerated by a
        small margin, because mail servers disagree by seconds and a code rejected for arriving 4
        seconds "early" is a false negative the operator would have to unpick.
        """
        skew = timedelta(seconds=90)
        return (self.requested_at - skew) <= received_at <= self.expires_at


@dataclass(frozen=True)
class VerificationOutcome:
    kind: VerificationKind
    reason: str
    code: Optional[str] = None
    link: Optional[str] = None
    confidence: float = 0.0
    #: Stable identity of what was extracted, for idempotency: applying the same code twice must be a
    #: no-op, and this is the key that makes that checkable.
    fingerprint: str = ""

    @property
    def usable(self) -> bool:
        return self.kind is not VerificationKind.NONE


def _host_of(sender: str) -> str:
    return sender.rsplit("@", 1)[-1].strip().lower() if "@" in sender else ""


def _registrable(host: str) -> str:
    """The registrable domain, so `mail.`, `no-reply.` and `www.` subdomains compare equal.

    Portals do not send from the host you are browsing: the page is `www.unpartnerportal.org` and the
    mail is from `mail.unpartnerportal.org`. Comparing full hosts REFUSED a genuine verification
    message - measured live - and the run then waited out its whole window for a code that had
    already arrived.
    """
    parts = [p for p in (host or "").strip(".").lower().split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    # Two-label public suffixes (co.uk, com.au, org.za, ...) need three labels kept, or
    # `ngo.co.ug` and `evil.co.ug` would both reduce to `co.ug` and match each other.
    if len(parts[-1]) == 2 and parts[-2] in {
        "co", "com", "org", "net", "gov", "ac", "edu", "or", "ne", "go"
    }:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host_matches(sender_host: str, portal_host: str) -> bool:
    """Whether the mail plausibly came from the portal.

    Compared at the REGISTRABLE DOMAIN, not the full host, because portals send from a mail
    subdomain. That is still strict enough: `evil-unpartnerportal.org` and
    `unpartnerportal.org.evil.com` both reduce to domains that differ from `unpartnerportal.org`, so
    an impersonator is still refused - which a naive `endswith` would have allowed.
    """
    a = _registrable(sender_host)
    b = _registrable(portal_host)
    if not a or not b:
        return False
    return a == b


def _find_code(subject: str, body: str) -> Optional[str]:
    for pattern in _CODE_PATTERNS:
        match = pattern.search(body) or pattern.search(subject)
        if not match:
            continue
        groups = [g for g in match.groups() if g]
        candidate = "".join(groups)
        digits = re.sub(r"\D", "", candidate)
        # A MIXED code like "A1B2C3" has digits but only three of them, so testing the digit count
        # alone rejected it: `digits` was truthy, the length check failed, and the alphanumeric
        # branch was skipped because it only ran when there were NO digits at all.
        if any(ch.isalpha() for ch in candidate) and 5 <= len(candidate) <= 10:
            return candidate.upper()
        if digits and CODE_MIN <= len(digits) <= CODE_MAX:
            return digits
    return None


def _find_link(body: str, portal_host: str) -> Optional[str]:
    best: Optional[str] = None
    for raw in _LINK_PATTERN.findall(body or ""):
        url = raw.rstrip(".,;:)\"'")
        lowered = url.lower()
        if any(bad in lowered for bad in _LINK_DENY):
            continue
        if not any(marker in lowered for marker in _LINK_MARKERS):
            continue
        # A link on the portal's own host outranks a link on a third-party mailer, but a mailer's
        # click-tracker IS the working link in most templates, so it is accepted rather than rejected.
        if portal_host and portal_host.lower() in lowered:
            return url
        best = best or url
    return best


def is_verification_message(subject: str, body: str) -> bool:
    """Whether this looks like a verification message at all.

    Cheap pre-filter. It decides nothing on its own - `extract` still requires the request, the
    mailbox and the window - but it keeps unrelated mail out of the extractor's way.
    """
    haystack = f"{subject or ''}\n{body or ''}".lower()
    if any(term in haystack for term in _VERIFY_TERMS):
        return True
    # A message with a code-shaped number AND a link containing a token is verification-shaped even
    # when the wording is unusual or in another language.
    return bool(re.search(r"\b\d{4,8}\b", haystack) and "token=" in haystack)


def extract(
    *,
    subject: str,
    body: str,
    sender: str,
    recipients: Iterable[str],
    received_at: datetime,
    request: VerificationRequest,
) -> VerificationOutcome:
    """Take the code or link out of one message, or say precisely why it was not usable.

    The reason is returned rather than a bare None because an unattended flow that silently does
    nothing is indistinguishable from a broken one, and the operator needs to know whether to wait,
    to retry the registration, or to look at the mail.
    """
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)

    # 1. TENANT AND MAILBOX. The code must have arrived at the mailbox this request nominated. This
    #    is the check that keeps one organisation's verification from satisfying another's.
    accepted = [r for r in (recipients or []) if request.accepts_address(r)]
    if not accepted:
        return VerificationOutcome(
            VerificationKind.NONE,
            "the message did not arrive at the mailbox this verification was sent to",
        )

    # 2. WINDOW. Before the request is a previous attempt's code; after expiry it is dead.
    if not request.within_window(received_at):
        if received_at > request.expires_at:
            return VerificationOutcome(VerificationKind.NONE, "the verification request has expired")
        return VerificationOutcome(
            VerificationKind.NONE,
            "the message arrived before the verification was requested; it belongs to an earlier attempt",
        )

    # 3. SENDER. An unrelated sender that happens to know the address must not complete the flow.
    sender_host = _host_of(sender or "")
    if not _host_matches(sender_host, request.portal_host):
        return VerificationOutcome(
            VerificationKind.NONE,
            f"the sender {sender_host or 'unknown'!r} is not the portal {request.portal_host!r}",
        )

    if not is_verification_message(subject, body):
        return VerificationOutcome(
            VerificationKind.NONE, "the message does not read as a verification message"
        )

    code = _find_code(subject or "", body or "")
    link = _find_link(body or "", request.portal_host)

    if code:
        return VerificationOutcome(
            VerificationKind.CODE, "a verification code was found in the portal's message",
            code=code, confidence=0.9 if link else 0.95,
            fingerprint=f"{request.org_id}:CODE:{code}",
        )
    if link:
        return VerificationOutcome(
            VerificationKind.LINK, "a verification link was found in the portal's message",
            link=link, confidence=0.8,
            fingerprint=f"{request.org_id}:LINK:{link[:120]}",
        )
    return VerificationOutcome(
        VerificationKind.NONE,
        "the message reads as verification mail but no code or verification link was present",
    )


def describe() -> dict[str, Any]:
    return {
        "kinds": [k.value for k in VerificationKind],
        "code_length": [CODE_MIN, CODE_MAX],
        "requires": "a VerificationRequest: same org, same mailbox, inside the window, from the portal",
    }
