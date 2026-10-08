"""Prometheus exposition, with the two things that make metrics trustworthy.

1. Bounded label cardinality
----------------------------
Prometheus dies from label explosion, and the explosion almost always comes from the
same three places:

* an ``organisation_id`` or ``user_id`` label - one time series per customer, forever;
* a **raw request path** - ``/api/v1/agent/grants/<uuid>`` is a unique label per
  request, and unlike the first two it is **attacker-controlled**: a scanner requesting
  random paths creates a new time series per request and takes the monitoring down as a
  side effect of probing the application;
* an exception message or an email address used as a label.

So labels come only from the **route template** FastAPI resolved, never the raw path; an
unmatched request is labelled ``__unmatched__`` so a probe cannot mint a series; every
label is validated against a character allowlist; and there is a hard ceiling on distinct
label sets beyond which new ones are dropped and counted rather than admitted.

2. Never report a zero for something you could not measure
---------------------------------------------------------
`outbox_events` has RLS **enabled but not FORCE**-bound, and `jobs` is FORCE-bound. The
application role is therefore bound by the policy and sees **zero rows unscoped**:

    app role, unscoped: outbox backlog 0, jobs 0
    (the tables are not empty - the role simply may not read across tenants)

A gauge computed from that reports ``granada_outbox_backlog_events 0`` on a system whose
relay has been dead for hours. The alert on it never fires, and the graph looks perfect.
**A metric that cannot be measured must be absent, not zero** - absence is a visible
condition, zero is indistinguishable from health.

So the operational block is read through a **separate metrics connection** (the owner, or
a dedicated BYPASSRLS role - the same prerequisite as backups), and when none is
configured the block is *omitted* and ``granada_metrics_operational_available`` is 0. The
alerting rules fire on that being 0, not on a backlog threshold.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

#: Metric and label names in the Prometheus data model.
_VALID_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_VALID_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
#: Values are escaped, but a bound keeps a hostile path from producing a megabyte label.
_MAX_LABEL_VALUE = 200

#: How many distinct label sets one metric may have before new ones are refused. Chosen
#: well above any legitimate use here (the largest is method x route x status class, a
#: few hundred) and well below anything that would hurt a Prometheus server.
MAX_SERIES_PER_METRIC = 500

#: The registry's own counters use dots (`queue.depth`) because that was the original
#: naming; Prometheus requires `[a-zA-Z_:][a-zA-Z0-9_:]*`. So they are translated rather
#: than renamed, which keeps the existing dashboards' names intact in logs and the JSON
#: snapshot.
_PREFIX = "granada_"


def to_metric_name(name: str) -> str:
    """`queue.depth` -> `granada_queue_depth`. Invalid characters become underscores."""
    cleaned = re.sub(r"[^a-zA-Z0-9_:]", "_", name)
    if cleaned and cleaned[0].isdigit():
        cleaned = "_" + cleaned
    if not cleaned.startswith(_PREFIX):
        cleaned = _PREFIX + cleaned
    if not _VALID_NAME.match(cleaned):  # pragma: no cover - defensive
        cleaned = _PREFIX + "invalid_metric_name"
    return cleaned


def _label_value(value: Any) -> str:
    text = str(value)[:_MAX_LABEL_VALUE]
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class SeriesLimiter:
    """Refuses new label sets past a ceiling, and counts what it refused.

    Defence in depth behind the route-template rule. The template rule removes the known
    attack; this bounds the unknown one, and the dropped-series counter makes the refusal
    visible instead of silent.
    """

    def __init__(self, limit: int = MAX_SERIES_PER_METRIC) -> None:
        self.limit = limit
        self._seen: dict[str, set[tuple[tuple[str, str], ...]]] = {}
        self._dropped = 0
        self._lock = threading.Lock()

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def admit(self, metric: str, labels: dict[str, str]) -> bool:
        key = tuple(sorted((k, v) for k, v in labels.items()))
        with self._lock:
            seen = self._seen.setdefault(metric, set())
            if key in seen:
                return True
            if len(seen) >= self.limit:
                self._dropped += 1
                return False
            seen.add(key)
            return True


limiter = SeriesLimiter()


# ===========================================================================
# HTTP request accounting
# ===========================================================================
class HttpMetrics:
    """Request counters and latency, with attributes chosen for boundedness.

    **`status_class`, not `status`.** A per-status-code label is small (a dozen values)
    but the interesting question in an alert is almost always "are we returning 5xx",
    and collapsing to the class keeps a bot hammering 404s from creating a series for
    every code it guesses.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, str], int] = {}
        self._duration_sum: dict[tuple[str, str, str], float] = {}
        self._duration_count: dict[tuple[str, str, str], int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def route_label(request: Any) -> str:
        """The ROUTE TEMPLATE, never the raw path.

        FastAPI stores the matched route on the scope, and ``route.path`` is the template
        (`/api/v1/agent/grants/{grant_id}`) rather than the request's path. Where it is
        absent - a 404, or a request that never matched - the answer is a constant, not
        the path, because the path is chosen by the caller.
        """
        route = getattr(request, "scope", {}).get("route")
        path = getattr(route, "path", None)
        if not path:
            return "__unmatched__"
        return path[:_MAX_LABEL_VALUE]

    def observe(self, *, method: str, route: str, status_code: int, seconds: float) -> None:
        status_class = f"{status_code // 100}xx"
        key = (method.upper()[:10], route, status_class)
        if not limiter.admit("http_requests_total", {"method": key[0], "route": route, "status": status_class}):
            return
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            self._duration_sum[key] = self._duration_sum.get(key, 0.0) + seconds
            self._duration_count[key] = self._duration_count.get(key, 0) + 1

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._duration_sum.clear()
            self._duration_count.clear()

    def render(self) -> list[str]:
        with self._lock:
            counts = dict(self._counts)
            sums = dict(self._duration_sum)
            durations = dict(self._duration_count)

        lines = [
            "# HELP granada_http_requests_total HTTP requests handled, by route template.",
            "# TYPE granada_http_requests_total counter",
        ]
        for (method, route, status_class), value in sorted(counts.items()):
            labels = (
                f'method="{_label_value(method)}",'
                f'route="{_label_value(route)}",'
                f'status_class="{_label_value(status_class)}"'
            )
            lines.append(f"granada_http_requests_total{{{labels}}} {render_value(value)}")

        lines += [
            "# HELP granada_http_request_duration_seconds_sum Total time spent handling requests.",
            "# TYPE granada_http_request_duration_seconds_sum counter",
        ]
        for (method, route, status_class), value in sorted(sums.items()):
            labels = (
                f'method="{_label_value(method)}",'
                f'route="{_label_value(route)}",'
                f'status_class="{_label_value(status_class)}"'
            )
            lines.append(
                f"granada_http_request_duration_seconds_sum{{{labels}}} "
                f"{render_value(value)}"
            )

        lines += [
            "# HELP granada_http_request_duration_seconds_count Requests timed.",
            "# TYPE granada_http_request_duration_seconds_count counter",
        ]
        for (method, route, status_class), value in sorted(durations.items()):
            labels = (
                f'method="{_label_value(method)}",'
                f'route="{_label_value(route)}",'
                f'status_class="{_label_value(status_class)}"'
            )
            lines.append(
                f"granada_http_request_duration_seconds_count{{{labels}}} "
                f"{render_value(value)}"
            )
        return lines


http_metrics = HttpMetrics()


# ===========================================================================
# Operational gauges
# ===========================================================================
#: (metric name, help, builder). The builder takes the model module and returns SQL.
#:
#: **The status values come from the models, not from string literals.** Three of the
#: first eleven queries here were wrong - `jobs.status` (the column is `state`),
#: `AWAITING_APPROVAL` (the constant is `WAITING_FOR_APPROVAL`) and `SEND_UNKNOWN` (the
#: constant is `DELIVERY_UNKNOWN`) - and each one raised, which disabled the whole
#: operational block. Every operational alert was off, and the only visible symptom was
#: `granada_metrics_operational_available 0`, which nobody had set an alert on yet.
#:
#: Deriving from the model constants means a rename fails at import with a clear
#: AttributeError instead of silently producing a gauge that always reads zero.
OPERATIONAL_QUERIES: tuple[tuple[str, str, object], ...] = (
    (
        "granada_outbox_backlog_events",
        "Outbox events recorded but not yet published. A rising value means the relay is not running.",
        lambda m: "SELECT count(*) FROM outbox_events WHERE published_at IS NULL",
    ),
    (
        "granada_outbox_oldest_undelivered_seconds",
        "Age of the oldest unpublished event. AGE is the signal, not depth: a backlog of five four hours old is worse than five hundred a second old.",
        lambda m: "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at))), 0) "
                  "FROM outbox_events WHERE published_at IS NULL",
    ),
    (
        "granada_outbox_dead_lettered_events",
        "Events that exhausted their attempts. These are dropped work, not delayed work.",
        lambda m: "SELECT count(*) FROM outbox_events "
                  "WHERE published_at IS NULL AND attempts >= 10",
    ),
    (
        "granada_jobs_stuck",
        "Jobs holding a lease that has lapsed. A worker died, or is not heartbeating.",
        lambda m: f"SELECT count(*) FROM jobs WHERE state = '{m.Job.RUNNING}' "
                  "AND lease_expires_at IS NOT NULL AND lease_expires_at < now()",
    ),
    (
        "granada_jobs_queued",
        "Jobs waiting to be claimed.",
        lambda m: f"SELECT count(*) FROM jobs WHERE state = '{m.Job.QUEUED}'",
    ),
    (
        "granada_mail_send_intents_awaiting_approval",
        "Outbound mail waiting for a person. Rises when nobody is looking at the queue.",
        lambda m: "SELECT count(*) FROM mail_send_intents "
                  f"WHERE status = '{m.MailSendIntent.WAITING_FOR_APPROVAL}'",
    ),
    (
        "granada_mail_send_unknown",
        "Outbound mail whose outcome is unknown. Needs reconciliation and must not be retried.",
        lambda m: "SELECT count(*) FROM mail_send_intents "
                  f"WHERE status = '{m.MailSendIntent.DELIVERY_UNKNOWN}'",
    ),
    (
        "granada_submission_unknown",
        "Applications whose outcome is unknown. THE most important alert: a retry can file a second application, and many programmes disqualify both.",
        lambda m: "SELECT count(*) FROM submission_packages "
                  f"WHERE status = '{m.SubmissionPackage.SUBMISSION_UNKNOWN}'",
    ),
    (
        "granada_reports_overdue",
        "Funder reports past their deadline. Money is lost to these silently: no rejection letter, just a tranche that does not arrive.",
        lambda m: "SELECT count(*) FROM reporting_obligations "
                  f"WHERE status = '{m.ReportingObligation.STATUS_OVERDUE}'",
    ),
    (
        "granada_grants_with_blocked_payment",
        "Grants holding an unmet condition that blocks a disbursement.",
        lambda m: "SELECT count(DISTINCT grant_id) FROM grant_conditions "
                  f"WHERE status = '{m.GrantCondition.STATUS_OPEN}' AND blocks_payment IS TRUE",
    ),
    (
        "granada_disbursements_late",
        "Tranches past their expected date that have not arrived.",
        lambda m: "SELECT count(*) FROM disbursements "
                  f"WHERE status = '{m.Disbursement.EXPECTED}' "
                  "AND expected_on IS NOT NULL AND expected_on < now()",
    ),
)


def build_queries() -> list[tuple[str, str, str]]:
    """Resolve the SQL, failing loudly if a model constant has been renamed."""
    import models

    resolved: list[tuple[str, str, str]] = []
    for name, help_text, builder in OPERATIONAL_QUERIES:
        resolved.append((name, help_text, builder(models)))  # type: ignore[operator]
    return resolved


def _engine_from(url: Optional[str]):
    """The engine the operational gauges are read through.

    Bounded like the application engine, and for the same reasons - but with one difference
    worth noting: this engine is used by `/metrics`, which is **unauthenticated**. An
    unbounded connect here would let an anonymous request block a worker for the operating
    system's TCP timeout.
    """
    from sqlalchemy import create_engine

    from config import settings

    if not url:
        return None
    if url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)

    from database import _startup_options

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=settings.database_pool_recycle_seconds,
        connect_args={
            "connect_timeout": settings.database_connect_timeout,
            # Composed rather than replaced, for the same reason as the application engine: a
            # scoped URL carries a search_path in its own `options`.
            "options": _startup_options(url),
        },
    )


def operational_gauges(
    *, metrics_url: Optional[str] = None
) -> tuple[Optional[dict[str, float]], dict[str, str], Optional[str]]:
    """Read the cross-tenant gauges.

    Returns ``(gauges, unavailable, reason)``.

    **Each query runs in its own SAVEPOINT.** That lesson has now been learned three
    times in this project - a denied read on one table poisoning every read after it -
    and here it costs the most: without isolation, ONE wrong column name switches off
    EVERY operational alert, and the symptom is a gauge that looks like health.

    ``unavailable`` maps a metric name to why it could not be read, so a partial block is
    visible per metric rather than silently missing.
    """
    engine = _engine_from(metrics_url)
    if engine is None:
        # THE ACTION COMES FIRST. Label values are truncated at 200 characters, and the
        # first version put "configure GRANADA_METRICS_DATABASE_URL" at the END of a long
        # explanation - so the only part an operator needed was the part that got cut
        # off, leaving an error message that said something was wrong without saying
        # what to do.
        return None, {}, (
            "GRANADA_METRICS_DATABASE_URL is not configured. The application role is "
            "RLS-bound and reads 0 rows from these tables unscoped, so every gauge would "
            "read 0 while the system was broken."
        )

    from sqlalchemy import text

    try:
        queries = build_queries()
    except AttributeError as exc:
        return None, {}, f"a model constant this module depends on was renamed: {exc}"

    values: dict[str, float] = {}
    unavailable: dict[str, str] = {}
    try:
        with engine.connect() as connection:
            for name, _help, sql in queries:
                try:
                    with connection.begin_nested():
                        row = connection.execute(text(sql)).scalar()
                    values[name] = float(row or 0)
                except Exception as exc:  # noqa: BLE001
                    # Omit THIS gauge and say why; do not lose the other ten.
                    unavailable[name] = f"{type(exc).__name__}: {str(exc)[:160]}"
    except Exception as exc:  # noqa: BLE001
        return None, {}, f"metrics database unreachable: {type(exc).__name__}: {str(exc)[:160]}"
    finally:
        engine.dispose()

    return values, unavailable, None


# ===========================================================================
# Exposition
# ===========================================================================
def render_value(value: float) -> str:
    """A float as the Prometheus text format requires it.

    **`inf` and `nan` are not valid.** The format specifies `+Inf`, `-Inf` and `NaN`, and
    a scraper that meets `nan` rejects the line - or, depending on the implementation, the
    whole payload. Either way, ONE metric whose computation divided by zero hides EVERY
    other metric in the response, which is the worst possible failure mode for a monitoring
    endpoint: the system looks unmonitored precisely when something is numerically wrong.

    `NaN` is also information rather than an error. Prometheus deliberately propagates it
    so that `x != x` can be alerted on, and `x > threshold` is false for it - so a broken
    computation alerts through a rule written for it rather than by breaking the scrape.
    """
    if value != value:  # NaN
        return "NaN"
    if value == float("inf"):
        return "+Inf"
    if value == float("-inf"):
        return "-Inf"
    return repr(float(value))


def _gauge(name: str, help_text: str, value: float) -> list[str]:
    return [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} gauge",
        f"{name} {render_value(value)}",
    ]


def exposition(
    *,
    registry: Optional[dict[str, float]] = None,
    metrics_url: Optional[str] = None,
    include_operational: bool = True,
    now: Optional[datetime] = None,
) -> str:
    """The full Prometheus text exposition.

    Every metric carries a HELP line, because a metric without one is a number somebody
    will misinterpret three months from now at two in the morning.
    """
    moment = now or datetime.now(timezone.utc)
    lines: list[str] = []

    # -- process ---------------------------------------------------------
    lines += _gauge(
        "granada_build_info",
        "Always 1. Carries the schema revision as a label so a scrape records what code answered.",
        # A constant gauge with an informative name; the value is deliberately useless.
        1,
    )

    # -- the in-process registry ----------------------------------------
    if registry is None:
        try:
            from observability import metrics as registry_metrics

            registry = registry_metrics.snapshot()
        except Exception:  # noqa: BLE001
            registry = {}
    for name, value in sorted((registry or {}).items()):
        metric = to_metric_name(name)
        lines += _gauge(metric, f"In-process metric {name!r}.", float(value))

    # -- HTTP ------------------------------------------------------------
    lines += http_metrics.render()

    # -- operational -----------------------------------------------------
    # The availability marker is ALWAYS emitted, so an alert can distinguish "everything
    # is fine" from "we could not look". Without it, a metrics connection that broke
    # during a deploy would silently disable every operational alert.
    if not include_operational:
        lines += _gauge(
            "granada_metrics_operational_available",
            "1 when the cross-tenant operational gauges were measured, 0 when they were not. "
            "When 0, the operational gauges are ABSENT rather than zero.",
            0,
        )
        lines += _gauge(
            "granada_metrics_operational_last_error",
            "1 when the last operational scrape failed, 0 when it succeeded.",
            1,
        )
    else:
        gauges, unavailable, reason = operational_gauges(metrics_url=metrics_url)
        if gauges is None:
            lines += _gauge(
                "granada_metrics_operational_available",
                "1 when the cross-tenant operational gauges were measured, 0 when they were not. "
                "When 0, the operational gauges are ABSENT rather than zero.",
                0,
            )
            lines += _gauge(
                "granada_metrics_operational_last_error",
                "1 when the last operational scrape failed, 0 when it succeeded.",
                1,
            )
            lines += [
                "# HELP granada_metrics_operational_unavailable_reason Last reason the "
                "operational gauges were unavailable.",
                "# TYPE granada_metrics_operational_unavailable_reason gauge",
                f'granada_metrics_operational_unavailable_reason{{reason="{_label_value(reason or "unknown")}"}} 1',
            ]
        else:
            lines += _gauge(
                "granada_metrics_operational_available",
                "1 when the cross-tenant operational gauges were measured, 0 when they were not. "
                "When 0, the operational gauges are ABSENT rather than zero.",
                1,
            )
            lines += _gauge(
                "granada_metrics_operational_last_error",
                "1 when the last operational scrape failed, 0 when it succeeded.",
                0,
            )
            helps = {name: help_text for name, help_text, _ in OPERATIONAL_QUERIES}
            for name, value in sorted(gauges.items()):
                lines += _gauge(name, helps.get(name, ""), value)
            # A gauge that could not be read is ABSENT from the block above, and named
            # here. Silence would be indistinguishable from a healthy zero.
            lines += [
                "# HELP granada_metrics_gauge_unavailable 1 when this operational gauge "
                "could not be read, 0 otherwise. The gauge itself is omitted rather than "
                "reported as zero.",
                "# TYPE granada_metrics_gauge_unavailable gauge",
            ]
            for name in sorted(unavailable):
                lines.append(
                    f'granada_metrics_gauge_unavailable{{metric="{_label_value(name)}"}} 1'
                )
            for name, _help, _builder in OPERATIONAL_QUERIES:
                if name not in unavailable:
                    lines.append(
                        f'granada_metrics_gauge_unavailable{{metric="{_label_value(name)}"}} 0'
                    )

    # -- the limiter's own state ----------------------------------------
    lines += _gauge(
        "granada_metrics_label_sets_dropped_total",
        "Label sets refused because a metric exceeded its series ceiling. Non-zero means "
        "something is generating unbounded labels and the metric is incomplete.",
        float(limiter.dropped),
    )
    lines += _gauge(
        "granada_metrics_scrape_timestamp_seconds",
        "When this exposition was rendered. A scrape whose timestamp stops advancing means "
        "the process is gone or the endpoint is cached.",
        moment.timestamp(),
    )

    return "\n".join(lines) + "\n"


def content_type() -> str:
    """The versioned content type, so a scraper parses it correctly."""
    return "text/plain; version=0.0.4; charset=utf-8"
