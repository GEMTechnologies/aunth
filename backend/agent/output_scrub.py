"""Scrub known secrets out of anything the browser worker is about to emit.

THE LEAK PATH, TRACED THROUGH THE CODE RATHER THAN IMAGINED
-----------------------------------------------------------
1. `PlaywrightProvider.authenticate` does `self._page.fill("[name='password']", password)`.
2. `observe()` reads the portal's rendered validation messages into `PageState.validation_messages`.
3. The runtime's report carries them into `report.to_dict()`.
4. `main()` prints that dict as JSON on stdout.

So a portal answering a failed login with "the password 'hunter2' was not recognised" puts the password
in the worker's OUTPUT - captured by the caller, written into a job outcome, and persisted. The
credential never had to be logged; the page only had to repeat it back.

That is not hypothetical: echoing the submitted value is one of the most common validation shapes there
is, and the test portal is not the only portal the worker will ever meet.

WHY THE EXISTING REDACTOR DOES NOT COVER THIS
---------------------------------------------
`agent/redaction.py` is TEXT redaction for messages: `redact(text)`, `digest`, `minimize` - built for
mail and model prompts. It takes a string and returns a `RedactionResult`. It is not a structured
scrubber, and it does not know the portal password, which arrives from the worker's environment rather
than from settings. `observability` likewise redacts SETTINGS secrets from LOG records; stdout here is
a RETURN VALUE, not a log line. Three different exits, and only this one was unguarded.

WHAT IS SCRUBBED

Every registered value, anywhere in the structure, at any depth - including inside lists of validation
messages and nested dicts, because that is exactly where a portal's echoed string sits.

SHORT VALUES ARE REPORTED, NOT SCRUBBED
---------------------------------------
Replacing every occurrence of a 3-character secret corrupts unrelated text while protecting little, so
values below a minimum length are SKIPPED and REPORTED. A short credential becomes a known gap rather
than a silent one.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

#: Below this, replacement does more damage than it prevents.
MIN_SCRUBBABLE_LENGTH = 6

#: Carries no hint of the value - not its length, not its first character.
REDACTED = "[redacted]"


class OutputScrubber:
    """Holds the values to remove. One per run, so one run's secrets cannot reach another's output."""

    def __init__(self, secrets: Optional[Iterable[str]] = None) -> None:
        self._values: list[str] = []
        self.unscrubbable: list[str] = []
        for value in secrets or ():
            self.add(value)

    def add(self, value: Optional[str], *, name: str = "secret") -> None:
        """Register a value.

        Empty and whitespace-only values are ignored: scrubbing `""` would replace at every character
        boundary and destroy the output while hiding nothing.
        """
        if value is None:
            return
        text = str(value)
        if not text.strip():
            return
        if len(text) < MIN_SCRUBBABLE_LENGTH:
            label = f"{name} (len {len(text)})"
            if label not in self.unscrubbable:
                self.unscrubbable.append(label)
            return
        if text not in self._values:
            self._values.append(text)

    def scrub(self, value: Any) -> Any:
        """Return the same SHAPE with every registered value replaced.

        Keys stay keys, list lengths stay lengths, types stay types. A scrubbed outcome is still a valid
        outcome - redaction that turned a dict into a string would be a different bug.
        """
        if isinstance(value, str):
            return self._scrub_text(value)
        if isinstance(value, dict):
            return {
                (self._scrub_text(k) if isinstance(k, str) else k): self.scrub(v)
                for k, v in value.items()
            }
        if isinstance(value, tuple):
            return tuple(self.scrub(item) for item in value)
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, set):
            return {self.scrub(item) for item in value}
        return value

    def _scrub_text(self, text: str) -> str:
        result = text
        for secret in self._values:
            if secret in result:
                result = result.replace(secret, REDACTED)
        return result

    def found_in(self, value: Any) -> list[str]:
        """Which registered secrets still appear. Used to VERIFY a scrub, not to trust it."""
        text = _flatten(value)
        return [f"secret[{i}]" for i, secret in enumerate(self._values) if secret in text]

    @property
    def registered(self) -> int:
        return len(self._values)

    def report(self) -> dict[str, Any]:
        """Names and counts only - never a value."""
        return {
            "secrets_registered": self.registered,
            "unscrubbable": list(self.unscrubbable),
            "min_length": MIN_SCRUBBABLE_LENGTH,
        }


def _flatten(value: Any) -> str:
    """Every string in the structure, joined. For verification only."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(k) + " " + _flatten(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(item) for item in value)
    return ""


def scrub(value: Any, *, secrets: Iterable[str]) -> Any:
    """One-shot convenience for a caller with no run-scoped scrubber."""
    return OutputScrubber(secrets).scrub(value)


def scrub_worker_environment(environ: Optional[dict] = None) -> list[str]:
    """The names (never the values) of the environment variables holding browser credentials.

    A helper rather than a constant, because the worker must not have a list of secrets it can print.
    It returns NAMES, which are safe, and the caller fetches the values itself.
    """
    import os

    env = os.environ if environ is None else environ
    return [name for name in ("GRANADA_PORTAL_PASSWORD", "GRANADA_PORTAL_USER") if env.get(name)]


def describe() -> dict[str, Any]:
    """The rule, stated where a reviewer will find it."""
    return {
        "leak_path": (
            "authenticate() fills the password field, observe() reads the portal's validation messages, "
            "report.to_dict() carries them, main() prints them as JSON - so a portal that ECHOES the "
            "submitted value puts it in the worker's output without anything being logged"
        ),
        "why_not_the_existing_redactor": (
            "agent/redaction.py redacts TEXT for messages (redact/digest/minimize) and takes a string; "
            "observability redacts SETTINGS secrets from LOG records. The portal password arrives from "
            "the worker's environment and stdout is a RETURN VALUE. Three exits; this covers the one "
            "that was open"
        ),
        "structure_preserved": "keys, list lengths and types are unchanged after scrubbing",
        "short_values": f"below {MIN_SCRUBBABLE_LENGTH} characters are REPORTED, not scrubbed",
        "replacement": REDACTED,
        "does_not_do": [
            "it does not scrub a secret it was not told about - registration is the caller's duty",
            "it does not make the output safe to publish; it removes known values only",
            "it does not redact the value from the PAGE, only from what the worker emits",
        ],
    }
