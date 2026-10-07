"""The Prometheus exporter, and the guards that make it trustworthy.

Two properties are tested here beyond the obvious, because they are the two that decide
whether monitoring can be believed:

1. **A metric that cannot be measured is absent, not zero.** The application role is
   RLS-bound, so it reads zero rows from `outbox_events` and `jobs` unscoped. A backlog
   gauge computed from that reads 0 on a system whose relay died hours ago, and the alert
   on it never fires while the graph looks perfect.

2. **No attacker-controlled label.** The HTTP counter is labelled by the ROUTE TEMPLATE
   FastAPI resolved, never the raw path, so a scanner requesting random URLs cannot mint
   a time series per request and take the monitoring down as a side effect of probing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import prometheus_metrics as pm  # noqa: E402


#: A complete exposition line: a metric name, an optional brace-delimited label set,
#: and a value.
#:
#: Three bugs in this one pattern, each of which made it reject VALID output:
#:
#: 1. The value class omitted the exponent's minus sign, so `1.2e-05` - valid, and common
#:    for latency sums - failed. A test that fails for the wrong reason gets "fixed" by
#:    loosening it until it checks nothing, so the pattern is kept precise instead.
#: 2. `inf`/`nan` were accepted implicitly; the format requires `+Inf`/`-Inf`/`NaN`, and
#:    the exporter now emits exactly those.
#: 3. **A label VALUE may contain braces**, because the label is a quoted string. The
#:    HTTP counter is labelled by route TEMPLATE, and a template with a path parameter
#:    (`/api/v1/agent/grants/{grant_id}`) contains them. A pattern of `\{[^}]*\}` stops at
#:    the inner brace and rejects a perfectly valid line - which is exactly how this was
#:    found, only in the full suite where the API tests had populated that route.
_LINE_RE = re.compile(
    r"^[a-zA-Z_:][a-zA-Z0-9_:]*"
    r'(\{\s*(?:[a-zA-Z_][a-zA-Z0-9_]*="(?:\\.|[^"\\])*"\s*,?\s*)*\})?'
    r" "
    r"(-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?|\+Inf|-Inf|NaN)$"
)


def _assert_valid_exposition(body: str) -> None:
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        assert _LINE_RE.match(line), f"not valid exposition syntax: {line!r}"




# ===========================================================================
# THE EXPOSITION FORMAT
# ===========================================================================
def test_the_exposition_parses_as_prometheus_text():
    """Every non-comment line must be `name value` or `name{labels} value`."""
    body = pm.exposition(include_operational=False)
    _assert_valid_exposition(body)


def test_every_metric_has_a_help_and_type_line():
    """A metric without a HELP line is a number somebody will misinterpret at two in the
    morning."""
    body = pm.exposition(include_operational=False)
    names = set()
    helps = set()
    types = set()
    for line in body.splitlines():
        if line.startswith("# HELP "):
            helps.add(line.split()[2])
        elif line.startswith("# TYPE "):
            types.add(line.split()[2])
        elif line and not line.startswith("#"):
            names.add(re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)", line).group(1))
    assert names <= helps, f"no HELP for {sorted(names - helps)}"
    assert names <= types, f"no TYPE for {sorted(names - types)}"


def test_the_content_type_is_the_versioned_one():
    assert pm.content_type().startswith("text/plain; version=0.0.4")
    assert pm.content_type().count("charset") <= 1, "charset declared twice"


# ===========================================================================
# A METRIC THAT CANNOT BE MEASURED IS ABSENT, NOT ZERO
# ===========================================================================
def test_with_no_metrics_connection_the_operational_gauges_are_absent():
    """THE property. The application role sees zero rows from these tables, so reporting
    zero would be a gauge that reads healthy on a broken system."""
    body = pm.exposition(include_operational=True, metrics_url=None)

    assert "granada_metrics_operational_available 0" in body
    for name, _help, _builder in pm.OPERATIONAL_QUERIES:
        assert f"{name} 0" not in body, (
            f"{name} was reported as zero although it could not be measured"
        )
        assert f"{name} " not in body, f"{name} appeared in the exposition at all"
    # And the reason is stated, so the fix is one line of reading.
    assert "granada_metrics_operational_unavailable_reason" in body
    assert "GRANADA_METRICS_DATABASE_URL" in body


def test_the_unavailable_marker_is_always_present():
    """So an alert can distinguish "everything is fine" from "we could not look"."""
    for body in (
        pm.exposition(include_operational=False),
        pm.exposition(include_operational=True, metrics_url=None),
    ):
        assert "granada_metrics_operational_available" in body
        assert "granada_metrics_operational_last_error" in body


def test_a_broken_metrics_connection_does_not_raise():
    """A metrics endpoint that 500s is worse than one reporting unavailability, because
    the scraper records `up 0` and the distinction is lost."""
    body = pm.exposition(
        include_operational=True,
        metrics_url="postgresql+psycopg2://nobody:wrong@127.0.0.1:1/nothing",
    )
    assert "granada_metrics_operational_available 0" in body


# ===========================================================================
# LABEL CARDINALITY
# ===========================================================================
def test_the_route_label_is_the_template_not_the_path():
    class FakeRoute:
        path = "/api/v1/agent/grants/{grant_id}"

    class FakeRequest:
        scope = {"route": FakeRoute()}

    assert pm.HttpMetrics.route_label(FakeRequest()) == "/api/v1/agent/grants/{grant_id}"


def test_an_unmatched_request_gets_a_constant_label():
    """THE attack this prevents: a scanner requesting random URLs would otherwise create
    one time series per request, and take the monitoring down as a side effect of probing
    the application."""

    class FakeRequest:
        scope = {}

    assert pm.HttpMetrics.route_label(FakeRequest()) == "__unmatched__"


def test_a_long_path_cannot_produce_an_unbounded_label():
    class FakeRoute:
        path = "/x/" + "a" * 5000

    class FakeRequest:
        scope = {"route": FakeRoute()}

    assert len(pm.HttpMetrics.route_label(FakeRequest())) <= pm._MAX_LABEL_VALUE


def test_a_hundred_thousand_distinct_paths_collapse_to_one_series():
    """Stated as the property that matters: the number of series is bounded by the number
    of ROUTES, not the number of requests."""
    metrics = pm.HttpMetrics()

    class FakeRoute:
        path = "/api/v1/agent/grants/{grant_id}"

    class FakeRequest:
        scope = {"route": FakeRoute()}

    route = pm.HttpMetrics.route_label(FakeRequest())
    for _ in range(100_000):
        metrics.observe(method="GET", route=route, status_code=200, seconds=0.001)

    body = "\n".join(metrics.render())
    series = [line for line in body.splitlines() if line.startswith("granada_http_requests_total{")]
    assert len(series) == 1, f"100,000 requests produced {len(series)} series"


def test_distinct_routes_do_produce_distinct_series():
    """The counterpart, so the test above cannot pass by labelling nothing."""
    metrics = pm.HttpMetrics()
    metrics.observe(method="GET", route="/a", status_code=200, seconds=0.001)
    metrics.observe(method="GET", route="/b", status_code=200, seconds=0.001)
    body = "\n".join(metrics.render())
    assert len([l for l in body.splitlines() if l.startswith("granada_http_requests_total{")]) == 2


def test_series_are_capped_and_the_refusal_is_counted():
    """Defence in depth behind the route-template rule: the known attack is removed, the
    unknown one is bounded, and the refusal is visible rather than silent."""
    limiter = pm.SeriesLimiter(limit=3)
    assert limiter.admit("m", {"a": "1"})
    assert limiter.admit("m", {"a": "2"})
    assert limiter.admit("m", {"a": "3"})
    assert not limiter.admit("m", {"a": "4"})
    # Existing series still work.
    assert limiter.admit("m", {"a": "1"})
    assert limiter.dropped == 1
    # A different metric has its own budget.
    assert limiter.admit("other", {"a": "1"})


def test_the_dropped_counter_is_exported():
    body = pm.exposition(include_operational=False)
    assert "granada_metrics_label_sets_dropped_total" in body


def test_status_is_collapsed_to_a_class():
    """A per-code label lets a bot hammering 404s create a series per guessed code."""
    metrics = pm.HttpMetrics()
    for code in (400, 403, 404, 405, 418, 429):
        metrics.observe(method="GET", route="/x", status_code=code, seconds=0.001)
    body = "\n".join(metrics.render())
    series = [l for l in body.splitlines() if l.startswith("granada_http_requests_total{")]
    assert len(series) == 1
    assert 'status_class="4xx"' in series[0]


def test_label_values_are_escaped():
    """A quote or newline in a label value would break the exposition."""
    escaped = pm._label_value('a"b\\c\nd')
    assert '"' not in escaped.replace('\\"', "")
    assert "\n" not in escaped


# ===========================================================================
# THE OPERATIONAL QUERIES REFERENCE REAL COLUMNS
# ===========================================================================
def test_every_operational_query_runs_against_the_migrated_schema(tmp_path):
    """THE guard for the bug that started this.

    Three of the first eleven queries named a column or a constant that does not exist -
    `jobs.status` (it is `state`), `AWAITING_APPROVAL` (it is `WAITING_FOR_APPROVAL`) and
    `SEND_UNKNOWN` (it is `DELIVERY_UNKNOWN`). Each raised, and because the block failed
    as a whole, EVERY operational alert was off.

    PostgreSQL-specific functions (`now()`, `EXTRACT`) do not exist in SQLite, so each
    query is checked for the failure that actually bit: an unknown table or column. A
    `no such column` or `no such table` error fails this test; anything else is a
    dialect difference and is not what is under test.
    """
    engine, session = make_sqlite_db(tmp_path, "metrics.sqlite")
    try:
        from sqlalchemy import text

        problems: list[str] = []
        for name, _help, sql in pm.build_queries():
            # Translate just enough for SQLite to parse the statement and resolve names.
            probe = sql.replace("now()", "CURRENT_TIMESTAMP")
            probe = re.sub(r"EXTRACT\(EPOCH FROM \(CURRENT_TIMESTAMP - min\(created_at\)\)\)",
                           "0", probe)
            probe = probe.replace("IS TRUE", "= 1")
            with engine.connect() as connection:
                try:
                    connection.execute(text(probe)).scalar()
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                    if "no such column" in message or "no such table" in message:
                        problems.append(f"{name}: {message.splitlines()[0]}")
        assert not problems, (
            "these operational queries reference something that does not exist, which "
            "would disable the whole operational block rather than one gauge: "
            + "; ".join(problems)
        )
    finally:
        session.close()
        engine.dispose()


def test_every_operational_query_resolves_its_model_constants():
    """A renamed constant must fail at import with a clear error, not produce a gauge
    that always reads zero."""
    queries = pm.build_queries()
    assert len(queries) == len(pm.OPERATIONAL_QUERIES)
    for name, _help, sql in queries:
        assert sql.strip().upper().startswith("SELECT"), name
        assert len(sql) > 20, name


def test_the_metric_names_are_valid_prometheus_identifiers():
    for name, _help, _builder in pm.OPERATIONAL_QUERIES:
        assert pm._VALID_NAME.match(name), f"{name} is not a valid metric name"


def test_dotted_registry_names_are_translated():
    """The in-process registry uses dots (`queue.depth`), which Prometheus does not
    accept. They are translated rather than renamed, so existing log dashboards keep
    working."""
    assert pm.to_metric_name("queue.depth") == "granada_queue_depth"
    assert pm.to_metric_name("job.succeeded") == "granada_job_succeeded"
    # Already-prefixed names are not double-prefixed.
    assert pm.to_metric_name("granada_x") == "granada_x"
    # A leading digit would otherwise be invalid.
    assert pm._VALID_NAME.match(pm.to_metric_name("1st.thing"))


# ===========================================================================
# INF AND NAN
# ===========================================================================
def test_infinity_and_nan_render_in_the_required_form():
    """`inf` and `nan` are NOT valid: the format specifies `+Inf`, `-Inf` and `NaN`. A
    scraper meeting `nan` rejects the line or the whole payload, so one metric whose
    computation divided by zero would hide EVERY other metric."""
    assert pm.render_value(float("inf")) == "+Inf"
    assert pm.render_value(float("-inf")) == "-Inf"
    assert pm.render_value(float("nan")) == "NaN"
    assert pm.render_value(1.5) == "1.5"
    assert pm.render_value(0) == "0.0"


def test_a_nan_metric_does_not_break_the_exposition():
    """The property, rather than the function: a NaN value must not make the payload
    unparseable."""
    import observability

    metrics = observability.metrics
    metrics.reset()
    try:
        metrics.set_gauge("probe.nan", float("nan"))
        metrics.set_gauge("probe.ok", 1.0)
        body = pm.exposition(include_operational=False)

        assert "granada_probe_nan NaN" in body
        # The other metric is still there, which is the point.
        assert "granada_probe_ok 1.0" in body
        # Checked as a VALUE, not as a substring: the metric is *named* probe_nan, so
        # `" nan" not in body` fails on the name alone - an assertion that rejects the
        # right output for the wrong reason.
        for line in body.splitlines():
            if line and not line.startswith("#"):
                value = line.rsplit(" ", 1)[-1]
                assert value != "nan", f"a bare nan value reached the exposition: {line!r}"
                assert value != "inf", f"a bare inf value reached the exposition: {line!r}"
    finally:
        metrics.reset()


def test_scientific_notation_is_accepted_by_the_syntax_check():
    """`1.2e-05` is valid and common for latency sums."""
    import observability

    metrics = observability.metrics
    metrics.reset()
    try:
        metrics.set_gauge("probe.tiny", 1.2e-05)
        body = pm.exposition(include_operational=False)
        assert "granada_probe_tiny 1.2e-05" in body
        _assert_valid_exposition(body)
    finally:
        metrics.reset()
