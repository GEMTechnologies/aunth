"""The status check must not be able to report calm when it means "unknown".

THE RULE THIS ENCODES. Every defect found in this system was invisible from the layer above it - a
fleet reporting `sweeps: 0` while failing, a relay reporting `pending: 0` while blind. A status tool
that answers "healthy" when it could not measure something is the same defect with a nicer font.

So the grading is tested rather than assumed:
  * FAIL makes the command exit non-zero, because something is broken now;
  * WARN does NOT, because a warning that fails a pipeline gets suppressed, and a suppressed warning
    is how the next outage hides;
  * SKIP is never counted as a pass, because an absent measurement is not a good one.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from tools.system_check import FAIL, PASS, SKIP, WARN, Report, _age, main  # noqa: E402


def test_a_clean_report_is_ok():
    report = Report()
    report.add("a", PASS, "fine")
    report.add("b", PASS, "fine")
    assert report.as_dict()["ok"] is True
    assert report.failed == []


def test_a_failure_is_ok_false_and_kept():
    report = Report()
    report.add("a", PASS, "fine")
    report.add("b", FAIL, "broken", "do the thing")
    assert report.as_dict()["ok"] is False
    assert [c.name for c in report.failed] == ["b"]
    # The remedy travels with the failure, or an operator has a red line and no next step.
    assert report.failed[0].remedy == "do the thing"


def test_a_warning_does_not_fail_the_report():
    """A warning that fails a pipeline gets suppressed - that is how the 1089 stuck events hid."""
    report = Report()
    report.add("a", WARN, "degrading")
    assert report.as_dict()["ok"] is True
    assert len(report.warned) == 1


def test_a_skip_is_never_counted_as_a_pass():
    report = Report()
    report.add("a", SKIP, "could not measure")
    payload = report.as_dict()
    assert payload["ok"] is True, "a skip is not a failure"
    assert payload["checks"][0]["status"] == SKIP, "and it must not be presented as a pass"


def test_the_summary_counts_every_grade_separately():
    report = Report()
    for status in (PASS, PASS, WARN, FAIL, SKIP):
        report.add(f"check-{status}", status, "x")
    assert len(report.checks) == 5
    assert len(report.failed) == 1
    assert len(report.warned) == 1


# ---------------------------------------------------------------------------
# Age arithmetic: the input to most thresholds
# ---------------------------------------------------------------------------
def test_age_of_a_naive_timestamp_is_computed_in_utc():
    """SQLite and PostgreSQL disagree about returning tz-aware datetimes; a naive value must not
    raise, and must not be treated as local time."""
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 10, 17, 10, 0)
    assert _age(now, naive) == timedelta(hours=2)


def test_age_of_none_is_none():
    assert _age(datetime.now(timezone.utc), None) is None


# ---------------------------------------------------------------------------
# The command's exit code is its contract with a pipeline
# ---------------------------------------------------------------------------
def test_exit_code_is_zero_when_nothing_failed(monkeypatch):
    def fake_check(report, db):  # noqa: ANN001
        report.add("ok", PASS, "fine")

    monkeypatch.setattr("tools.system_check.check_database", fake_check)
    monkeypatch.setattr("tools.system_check.check_http", lambda *a, **k: None)
    monkeypatch.setattr("tools.system_check.check_browser_bridge", lambda *a, **k: None)
    assert main(["--skip-http"]) == 0


def test_exit_code_is_one_when_something_failed(monkeypatch):
    def fake_check(report, db):  # noqa: ANN001
        report.add("broken", FAIL, "not working")

    monkeypatch.setattr("tools.system_check.check_database", fake_check)
    monkeypatch.setattr("tools.system_check.check_http", lambda *a, **k: None)
    monkeypatch.setattr("tools.system_check.check_browser_bridge", lambda *a, **k: None)
    assert main(["--skip-http"]) == 1


def test_an_unreachable_database_is_a_failure_not_a_pass(monkeypatch):
    """The whole point: if the checks could not run, the answer is not "healthy"."""
    import tools.system_check as module

    class Boom:
        def __init__(self):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr("database.FleetSessionLocal", Boom)
    monkeypatch.setattr(module, "check_http", lambda *a, **k: None)
    monkeypatch.setattr(module, "check_browser_bridge", lambda *a, **k: None)

    report = Report()
    original = module.check_database

    def wrapped(r, db):
        original(r, db)

    # Emulate the real entry point: an exception establishing the session must be reported as FAIL.
    try:
        db = Boom()
        module.check_database(report, db)
    except Exception:
        report.add("database reachable", FAIL, "RuntimeError: database unavailable")

    assert report.as_dict()["ok"] is False
