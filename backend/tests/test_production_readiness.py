"""Phase 10 production readiness: shutdown ordering, security headers, alerting rules.

The alerting test is the important one. **An alert naming a metric that does not exist
never fires**, and it fails silently — the rules file looks comprehensive, the dashboard
is green, and the only way to find out is an incident nobody was warned about.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import prometheus_metrics as pm  # noqa: E402

RULES = ROOT / "ops" / "prometheus" / "granada.rules.yml"


# ===========================================================================
# GRACEFUL SHUTDOWN
# ===========================================================================
@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    import main

    main._SHUTTING_DOWN.clear()
    yield TestClient(main.app)
    main._SHUTTING_DOWN.clear()


def test_readiness_fails_before_the_process_stops_answering(client):
    """THE property, and the one most services get wrong.

    A load balancer only stops routing once /readyz answers 503, and it needs time to
    notice. If the process simply exits, it keeps routing until the next health check and
    every request in that window is a connection refused - exactly the error graceful
    shutdown exists to prevent.

    Asserted as a TRANSITION rather than as "200 then 503". An earlier version asserted
    the ambient instance was ready first, which made the test depend on the scratch
    database having a schema - so it failed for the environment's reason rather than the
    behaviour's. The distinguishing mark is the detail: `shutting down; drain` can only
    come from the flag, whereas any dependency failure produces a different body.
    """
    import main

    main._SHUTTING_DOWN.set()
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "NOT_READY"
    assert "drain" in body["detail"], (
        f"the 503 did not come from the shutdown flag: {body}"
    )

    # And clearing the flag removes the shutdown reason, so the response is driven by
    # real dependency state rather than by a stuck flag.
    main._SHUTTING_DOWN.clear()
    after = client.get("/readyz")
    assert "drain" not in (after.json().get("detail") or "")


def test_liveness_stays_healthy_during_shutdown(client):
    """Live and ready answer different questions. The process IS alive while draining -
    killing it would abort the in-flight requests the drain exists to finish."""
    import main

    main._SHUTTING_DOWN.set()
    assert client.get("/livez").status_code == 200


def test_startup_clears_a_stale_shutdown_flag():
    """A process that begins life unready is a process that never receives traffic."""
    import main

    source = Path(main.__file__).read_text(encoding="utf-8")
    # Asserted on the source because this runs once, at startup, and the ordering in the
    # lifespan is the thing under test.
    assert "_SHUTTING_DOWN.clear()" in source
    clear_at = source.index("_SHUTTING_DOWN.clear()")
    set_at = source.index("_SHUTTING_DOWN.set()")
    assert clear_at < set_at, "the flag is set before it is cleared"


def test_the_drain_window_is_configurable_and_bounded():
    import main

    assert main.GRACEFUL_SHUTDOWN_SECONDS >= 0
    assert main.GRACEFUL_SHUTDOWN_SECONDS <= 60, (
        "a drain window longer than a minute delays every deploy for no benefit"
    )


# ===========================================================================
# SECURITY HEADERS
# ===========================================================================
@pytest.mark.parametrize(
    "header,expected",
    (
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
    ),
)
def test_security_headers_are_present(client, header, expected):
    assert client.get("/livez").headers.get(header) == expected


def test_headers_are_on_error_responses_too(client):
    """An error page is exactly where a browser is most likely to be persuaded to do
    something."""
    response = client.get("/livez")
    assert response.status_code == 200
    # And on a 404.
    missing = client.get("/definitely-not-a-route")
    assert missing.status_code == 404
    assert missing.headers.get("x-content-type-options") == "nosniff"
    assert missing.headers.get("x-frame-options") == "DENY"


def test_api_responses_are_not_cacheable(client):
    """Organisation data in a shared cache is a disclosure."""
    response = client.get("/api/v1/metrics")
    assert response.headers.get("cache-control") == "no-store"


def test_hsts_is_production_only(client):
    """Sending HSTS from a development origin pins the browser to https for localhost and
    breaks unrelated local work."""
    import main

    assert main.settings.app_env != "production"
    assert "strict-transport-security" not in client.get("/livez").headers


# ===========================================================================
# THE ALERTING RULES
# ===========================================================================
def _rules_text() -> str:
    assert RULES.exists(), f"no alerting rules at {RULES}"
    return RULES.read_text(encoding="utf-8")


def test_the_rules_file_parses_as_yaml():
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(_rules_text())
    assert "groups" in document
    assert document["groups"], "no rule groups"


def test_every_rule_has_an_expression_a_severity_and_a_summary():
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(_rules_text())
    for group in document["groups"]:
        for rule in group["rules"]:
            assert rule.get("expr"), f"{rule.get('alert')} has no expression"
            labels = rule.get("labels") or {}
            assert labels.get("severity") in {"critical", "warning"}, rule.get("alert")
            assert (rule.get("annotations") or {}).get("summary"), rule.get("alert")


def test_every_metric_an_alert_references_actually_exists():
    """THE guard. An alert naming a metric that does not exist never fires, and it fails
    silently: the rules file looks comprehensive, the dashboard is green, and the only way
    to find out is an incident nobody was warned about.
    """
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(_rules_text())

    known = {
        "granada_metrics_operational_available",
        "granada_metrics_operational_last_error",
        "granada_metrics_gauge_unavailable",
        "granada_metrics_label_sets_dropped_total",
        "granada_metrics_scrape_timestamp_seconds",
        "granada_build_info",
        "granada_http_requests_total",
        "granada_http_request_duration_seconds_sum",
        "granada_http_request_duration_seconds_count",
    } | {name for name, _help, _builder in pm.OPERATIONAL_QUERIES}

    referenced: set[str] = set()
    for group in document["groups"]:
        for rule in group["rules"]:
            referenced |= set(re.findall(r"\b(granada_[a-z0-9_]+)\b", rule["expr"]))

    unknown = referenced - known
    assert not unknown, (
        f"these metrics are referenced by alerts but are never exported, so the alerts "
        f"can never fire: {sorted(unknown)}"
    )


def test_the_unavailable_guard_is_alerted_on():
    """Without this rule the whole file is decoration: every other operational alert
    depends on gauges that can be silently absent, and 'no alerts' would mean nothing."""
    text = _rules_text()
    assert "GranadaOperationalMetricsUnavailable" in text
    assert "granada_metrics_operational_available == 0" in text
    body = text.split("GranadaOperationalMetricsUnavailable", 1)[1]
    assert "None of them can fire" in body or "cannot fire" in body or "means nothing" in body, (
        "the rule must say why it exists, or it will be deleted as noise"
    )


def test_the_most_consequential_alerts_fire_immediately():
    """A `for:` window on these is a decision to keep losing money for that long."""
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(_rules_text())
    immediate = {
        "GranadaSubmissionOutcomeUnknown",
        "GranadaFunderReportOverdue",
    }
    found = {}
    for group in document["groups"]:
        for rule in group["rules"]:
            found[rule.get("alert")] = rule.get("for")
    for name in immediate:
        assert name in found, f"{name} is missing"
        assert found[name] in ("0m", "0s"), (
            f"{name} waits {found[name]}; a delay here is a decision to keep losing "
            "money or an application for that long"
        )


def test_every_operational_gauge_that_matters_has_an_alert():
    """Not every gauge needs one, but the ones whose absence means silent financial loss
    do. Named explicitly so dropping one is a deliberate act."""
    text = _rules_text()
    for metric in (
        "granada_outbox_oldest_undelivered_seconds",
        "granada_jobs_stuck",
        "granada_submission_unknown",
        "granada_reports_overdue",
        "granada_disbursements_late",
        "granada_grants_with_blocked_payment",
    ):
        assert metric in text, f"no alert references {metric}"


def test_alert_rules_do_not_use_a_high_cardinality_label():
    """`$labels.organisation_id` in a summary would create an alert per customer and
    hammer the alertmanager with thousands of notifications for one outage."""
    text = _rules_text()
    for forbidden in ("$labels.organisation_id", "$labels.org_id", "$labels.user_id", "$labels.path"):
        assert forbidden not in text, f"{forbidden} in an alert annotation"
