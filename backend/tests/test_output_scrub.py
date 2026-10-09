"""A portal that echoes the submitted password must not put it in the worker's output.

THE PATH: authenticate() fills the password field -> observe() reads the portal's validation messages
-> report.to_dict() carries them -> main() prints them as JSON. Nothing has to be logged for the
credential to escape; the page only has to repeat it back.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.output_scrub import (  # noqa: E402
    MIN_SCRUBBABLE_LENGTH,
    REDACTED,
    OutputScrubber,
    describe,
    scrub,
    scrub_worker_environment,
)

SECRET = "hunter2-correct-horse"


# ===========================================================================
# THE LEAK PATH
# ===========================================================================
def test_a_password_echoed_in_a_validation_message_is_removed():
    """THE test, and the exact shape of the leak: a portal answering a failed login by quoting what was
    submitted."""
    outcome = {
        "status": "FAILED",
        "problems": [
            {"kind": "LOGIN_REJECTED", "detail": f"the password '{SECRET}' was not recognised"}
        ],
    }
    scrubbed = scrub(outcome, secrets=[SECRET])
    assert SECRET not in str(scrubbed)
    assert REDACTED in scrubbed["problems"][0]["detail"]


def test_a_password_echoed_deep_in_a_structure_is_removed():
    outcome = {"a": [{"b": [{"c": f"x{SECRET}y"}]}]}
    assert SECRET not in str(scrub(outcome, secrets=[SECRET]))


def test_a_password_in_a_VALIDATION_MESSAGE_LIST_is_removed():
    """Where it actually lands: PageState.validation_messages is a list of strings."""
    state = {"validation_messages": ["Required", f"'{SECRET}' is not valid", "Too short"]}
    scrubbed = scrub(state, secrets=[SECRET])
    assert SECRET not in str(scrubbed)
    assert scrubbed["validation_messages"][0] == "Required"
    assert len(scrubbed["validation_messages"]) == 3, "a message was dropped, not redacted"


def test_a_password_in_a_DICT_KEY_is_removed():
    """A form field named after its value is unusual but not impossible, and a scrubber that only
    walked values would miss it."""
    assert SECRET not in str(scrub({SECRET: "v"}, secrets=[SECRET]))


# ===========================================================================
# STRUCTURE IS PRESERVED
# ===========================================================================
def test_the_shape_and_types_are_unchanged():
    original = {"list": [1, 2], "tuple": (1, "x"), "dict": {"k": "v"}, "n": 5, "b": True, "none": None}
    scrubbed = scrub(original, secrets=[SECRET])
    assert isinstance(scrubbed, dict)
    assert isinstance(scrubbed["list"], list)
    assert isinstance(scrubbed["tuple"], tuple)
    assert scrubbed["n"] == 5 and scrubbed["b"] is True and scrubbed["none"] is None


def test_a_non_string_value_is_untouched():
    assert scrub({"n": 42, "f": 1.5}, secrets=[SECRET]) == {"n": 42, "f": 1.5}


def test_scrubbing_does_not_mutate_the_original():
    """The run may still need the real outcome, and a scrubber that edited in place would make the
    safety step destructive."""
    original = {"detail": SECRET}
    scrub(original, secrets=[SECRET])
    assert original["detail"] == SECRET


# ===========================================================================
# WHAT IS NOT SCRUBBED, AND WHY IT IS REPORTED
# ===========================================================================
def test_a_short_secret_is_REPORTED_rather_than_silently_skipped():
    """Replacing every occurrence of a 3-character value corrupts unrelated text while protecting
    little - so it is skipped, and the gap is named rather than hidden."""
    s = OutputScrubber(["abc"])
    assert s.registered == 0
    assert s.unscrubbable and "len 3" in s.unscrubbable[0]


def test_a_long_enough_secret_is_scrubbed():
    s = OutputScrubber([SECRET])
    assert s.registered == 1
    assert s.unscrubbable == []


def test_an_empty_value_is_ignored():
    """Scrubbing `''` would replace at every character boundary and destroy the output while hiding
    nothing."""
    s = OutputScrubber(["", "   ", None])
    assert s.registered == 0
    assert s.unscrubbable == []


def test_the_boundary_is_where_it_says_it_is():
    s = OutputScrubber(["x" * (MIN_SCRUBBABLE_LENGTH - 1)])
    assert s.registered == 0
    s2 = OutputScrubber(["x" * MIN_SCRUBBABLE_LENGTH])
    assert s2.registered == 1


# ===========================================================================
# VERIFY A SCRUB RATHER THAN TRUST IT
# ===========================================================================
def test_found_in_reports_a_secret_that_survived():
    s = OutputScrubber([SECRET])
    assert s.found_in({"d": SECRET}) == ["secret[0]"]
    assert s.found_in(s.scrub({"d": SECRET})) == []


def test_found_in_does_not_return_the_value():
    """A verification helper must not become the leak it exists to prevent."""
    s = OutputScrubber([SECRET])
    assert SECRET not in str(s.found_in({"d": SECRET}))


def test_the_report_names_no_value():
    s = OutputScrubber([SECRET, "short"])
    report = s.report()
    assert SECRET not in str(report)
    assert report["secrets_registered"] == 1
    assert report["unscrubbable"]


# ===========================================================================
# THE ENVIRONMENT HELPER RETURNS NAMES
# ===========================================================================
def test_the_environment_helper_returns_NAMES_only():
    """The worker must not have a function that hands back its own secrets."""
    names = scrub_worker_environment({"GRANADA_PORTAL_PASSWORD": SECRET, "GRANADA_PORTAL_USER": "ngo"})
    assert sorted(names) == ["GRANADA_PORTAL_PASSWORD", "GRANADA_PORTAL_USER"]
    assert SECRET not in str(names)


def test_an_absent_variable_is_not_named():
    assert scrub_worker_environment({"GRANADA_PORTAL_PASSWORD": ""}) == []


# ===========================================================================
# A RUN-SCOPED SCRUBBER
# ===========================================================================
def test_two_runs_do_not_share_secrets():
    """One scrubber per run: a shared one would mean one organisation's credential could redact text in
    another's outcome, which is a cross-tenant signal however small."""
    a = OutputScrubber(["org-a-secret-value"])
    b = OutputScrubber(["org-b-secret-value"])
    assert a.scrub("org-b-secret-value") == "org-b-secret-value"
    assert b.scrub("org-b-secret-value") == REDACTED


def test_the_boundary_is_stated():
    d = describe()
    assert "ECHOES" in d["leak_path"]
    assert "redaction.py" in d["why_not_the_existing_redactor"]
    assert "RETURN VALUE" in d["why_not_the_existing_redactor"]
    joined = " ".join(d["does_not_do"])
    assert "not told about" in joined
    assert "only from what the worker emits" in joined
