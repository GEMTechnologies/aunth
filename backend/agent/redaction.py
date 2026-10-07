"""Redaction and data minimisation for outbound model prompts.

Nothing in this module is a security boundary. It is a *reduction* in what
leaves the building, and it is deliberately conservative: it replaces
recognisable personal and credential data with stable placeholders so a prompt
can be reasoned about and digested, while accepting that a determined
adversary can defeat pattern matching.

Two design points worth stating, because both were choices rather than
obvious defaults:

**Placeholders are stable within one prompt.** ``[EMAIL_1]`` and ``[EMAIL_1]``
refer to the same address, so the model can still reason about "reply to the
same person". Renumbering per occurrence would destroy exactly the structure
the model needs.

**Redaction is idempotent.** Running it twice produces the same output, so a
prompt that passes through two code paths is not progressively mangled. This
is asserted by a test rather than assumed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# Ordered by specificity. A bare digit-run rule placed first would eat the
# numeric parts of IBANs and phone numbers before the specific rules saw them.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # IBAN before the generic digit rules: SS82 XXXX 0000 0000 0000 0000 00
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")),
    # Card-like: 13-19 digits, optionally separated in groups of 4.
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("PHONE", re.compile(r"(?<!\w)(?:\+\d{1,3}[ -]?)?(?:\(\d{1,4}\)[ -]?)?\d{3,4}[ -]?\d{3,4}(?!\w)")),
    ("SECRET", re.compile(r"\b(?:sk|pk|rk|ghp|gho|glpat|xox[baprs])[-_][A-Za-z0-9_\-]{16,}\b")),
    ("JWT", re.compile(r"\bey[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("BEARER", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE)),
    ("NATIONAL_ID", re.compile(r"\b(?:SSN|NIN|NINO)\s*[:#]?\s*[A-Z0-9\-]{6,}\b", re.IGNORECASE)),
)

# A placeholder must survive a second pass unchanged, so the rules above must
# not match the substituted form. They do not: no rule matches a bracketed
# upper-case token followed by digits.


@dataclass
class RedactionResult:
    """The redacted text plus a count of what was removed, by kind."""

    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return bool(self.counts)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def redact(text: str | None) -> RedactionResult:
    """Replace personal and credential data with stable placeholders.

    The same value always maps to the same placeholder within one call, so
    relationships inside the prompt survive.
    """
    if not text:
        return RedactionResult(text=text or "", counts={})

    counts: dict[str, int] = {}
    out = text

    for kind, pattern in _PATTERNS:
        seen: dict[str, str] = {}

        def _sub(match: re.Match[str], kind: str = kind, seen: dict[str, str] = seen) -> str:
            value = match.group(0)
            key = value.strip().lower()
            if key not in seen:
                seen[key] = f"[{kind}_{len(seen) + 1}]"
            return seen[key]

        out, n = pattern.subn(_sub, out)
        if n:
            counts[kind] = counts.get(kind, 0) + n

    return RedactionResult(text=out, counts=counts)


def digest(text: str | None) -> str:
    """SHA-256 of the UTF-8 text, or the digest of the empty string.

    Used for the ``prompt_digest`` / ``response_digest`` columns so an
    invocation can be matched to an input without retaining the input.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def minimize(text: str | None, *, max_chars: int = 120_000) -> str:
    """Redact, then bound the length.

    Truncation happens **after** redaction, and truncating rather than failing
    is deliberate: a long document should still be classifiable. The marker is
    explicit so downstream code can tell a truncated prompt from a complete
    one, which matters because a silently truncated prompt produces a
    confidently wrong answer.
    """
    result = redact(text)
    out = result.text
    if len(out) <= max_chars:
        return out
    return out[:max_chars] + f"\n[TRUNCATED: {len(out) - max_chars} characters omitted]"
