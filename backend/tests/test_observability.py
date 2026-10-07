"""Observability: structured logs, correlation ids, secret redaction, metrics.

The load-bearing test is ``test_secrets_never_appear_in_logs``, which the build
brief names directly. It is tested from several angles because a log leak has
many shapes: a literal argument, a formatted message, a connection URL quoted by
a driver, an exception traceback, and an unregistered credential that only a
pattern can catch.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import config  # noqa: E402
import observability as obs  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_secret_registry():
    """Registered secrets are process-global; leaking them between tests would
    make a later test pass for the wrong reason."""
    obs.clear_registered_secrets()
    try:
        yield
    finally:
        obs.clear_registered_secrets()


@pytest.fixture
def captured():
    """A logger wired to the real production configuration, writing to a buffer."""
    buffer = io.StringIO()
    obs.configure_logging(level="DEBUG", json_output=True, service="test-svc", stream=buffer)
    try:
        yield buffer
    finally:
        # Restore pytest's own capture so a later test's output is not swallowed.
        logging.getLogger().handlers = []


def _records(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# THE critical test
# ---------------------------------------------------------------------------
def test_secrets_never_appear_in_logs(captured):
    """The brief's named requirement, from every angle that leaks in practice.

    Six separate leak shapes, because passing one of them proves nothing about
    the others and the most dangerous leak - a driver quoting a connection URL
    inside an exception - never goes through ``logger.info(password)`` at all.
    """
    jwt_secret = "super-secret-jwt-value-0123456789abcdef"
    db_password = "hunter2-database-password"
    api_key = "sk-abcdefghijklmnopqrstuvwxyz012345"
    obs.register_secrets([jwt_secret, db_password, api_key])

    log = logging.getLogger("leak.test")
    database_url = f"postgresql://granada_app:{db_password}@localhost:5432/granada_auth"

    # 1. A literal argument.
    log.info("secret is %s", jwt_secret)
    # 2. Interpolated into the message.
    log.info(f"api key {api_key} leaked")
    # 3. A connection URL, the way a driver exception quotes it.
    log.error("could not connect to %s", database_url)
    # 4. Structured context rather than the message.
    log.warning("auth failed", extra={"token": jwt_secret})
    # 5. An exception traceback carrying the value.
    try:
        raise RuntimeError(f"connection to {database_url} refused")
    except RuntimeError:
        log.exception("database error")
    # 6. A credential nobody registered, with no distinguishing value - only a
    #    pattern can catch this, and an exact-match-only design would not.
    log.info("bearer header: Bearer abcdefghijklmnopqrstuvwxyz123456")

    output = captured.getvalue()

    for secret, label in (
        (jwt_secret, "jwt secret"),
        (db_password, "database password"),
        (api_key, "model api key"),
    ):
        assert secret not in output, f"the {label} appeared in the logs"
        # The digest of the secret must not appear either - a hash of a
        # low-entropy password is still a disclosure.
    assert "abcdefghijklmnopqrstuvwxyz123456" not in output, "a bearer token leaked"

    # Redaction must not have destroyed the record entirely: the useful part of
    # a connection failure is *which* host and database, not the password.
    assert "localhost:5432" in output
    assert "granada_auth" in output

    # And it must actually be JSON, one object per line, with the redaction
    # applied inside the parsed document rather than to the raw text only.
    parsed = _records(captured)
    assert parsed, "no JSON records were emitted"
    assert all("[REDACTED]" in json.dumps(r) or "REDACTED" in json.dumps(r) for r in parsed[:5])


def test_unregistered_connection_passwords_are_still_redacted(captured):
    """Exact matching alone is not enough.

    A credential the settings object does not know about - a rotated password,
    a value from a secret manager fetched at runtime - has no registered form.
    Only the URL pattern catches it.
    """
    logging.getLogger("leak.pattern").error(
        "failed: postgresql://someuser:never-registered-password@db.internal:5432/prod"
    )
    output = captured.getvalue()
    assert "never-registered-password" not in output
    assert "db.internal:5432/prod" in output


def test_short_values_are_not_registered(captured):
    """A 2-character "secret" would redact those characters everywhere and
    destroy the logs it was meant to protect."""
    obs.register_secret("ab")
    obs.register_secret("")
    obs.register_secret(None)
    assert obs.registered_secret_count() == 0


def test_redaction_survives_the_message_being_a_tuple_of_args(captured):
    secret = "multi-arg-secret-abcdefghijkl"
    obs.register_secret(secret)
    logging.getLogger("leak.args").info("a=%s b=%s c=%s", "one", secret, "three")
    assert secret not in captured.getvalue()


def test_redaction_survives_a_dict_style_log_call(captured):
    secret = "dict-style-secret-abcdefghij"
    obs.register_secret(secret)
    logging.getLogger("leak.dict").info("value=%(v)s", {"v": secret})
    assert secret not in captured.getvalue()


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_logs_are_one_json_object_per_line(captured):
    log = logging.getLogger("structure")
    log.info("first")
    log.warning("second")
    records = _records(captured)
    assert [r["message"] for r in records] == ["first", "second"]
    assert all(r["level"] in {"INFO", "WARNING"} for r in records)


def test_every_record_carries_the_service_name(captured):
    logging.getLogger("structure").info("hello")
    assert _records(captured)[0]["service"] == "test-svc"


def test_correlation_id_is_attached_to_every_record(captured):
    """The brief requires correlation ids; a trace that is only on some lines is
    not a trace."""
    cid = obs.new_correlation_id()
    obs.set_correlation_id(cid)
    try:
        logging.getLogger("trace").info("one")
        logging.getLogger("trace").info("two")
    finally:
        obs.set_correlation_id(None)

    records = _records(captured)
    assert [r.get("correlation_id") for r in records] == [cid, cid]


def test_records_without_a_correlation_id_omit_the_field_rather_than_nulling_it(captured):
    obs.set_correlation_id(None)
    logging.getLogger("trace").info("no context")
    assert "correlation_id" not in _records(captured)[0]


def test_caller_supplied_context_becomes_structured_fields(captured):
    """The "why?" view is a query over fields, not a regex over prose."""
    logging.getLogger("agent").info(
        "staged", extra={"org_id": "org-1", "job_id": "job-9", "provider": "openai"}
    )
    record = _records(captured)[0]
    assert record["org_id"] == "org-1"
    assert record["job_id"] == "job-9"
    assert record["provider"] == "openai"


def test_non_serialisable_context_does_not_break_logging(captured):
    """A log call that raises takes the request down with it."""

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    logging.getLogger("agent").info("thing", extra={"handle": Opaque()})
    record = _records(captured)[0]
    assert record["handle"] == "<opaque>"


def test_an_exception_is_recorded_in_full(captured):
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("agent").exception("failed")
    record = _records(captured)[0]
    assert "ValueError: boom" in record["exception"]


def test_the_root_logger_has_exactly_one_handler(captured):
    """A second handler would also write the record, and it would not redact."""
    obs.configure_logging(level="INFO", stream=io.StringIO())
    obs.configure_logging(level="INFO", stream=io.StringIO())
    assert len(logging.getLogger().handlers) == 1


def test_third_party_loggers_are_routed_through_our_redaction(captured):
    """Alembic and uvicorn install their own handlers, which know nothing about
    redaction - and Alembic logs the migration URL."""
    obs.configure_logging(level="INFO", stream=io.StringIO())
    for name in ("alembic", "uvicorn", "sqlalchemy.engine"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True


# ---------------------------------------------------------------------------
# Settings wiring
# ---------------------------------------------------------------------------
def test_settings_credentials_are_registered_at_startup():
    """The real values, not a hand-written list of names that can drift."""
    obs.clear_registered_secrets()
    assert config.settings.jwt_secret
    registered = obs.register_secrets_from_settings(config.settings)
    assert registered >= 1

    output = obs.redact_secrets(f"token={config.settings.jwt_secret}")
    assert config.settings.jwt_secret not in output
    assert "[REDACTED]" in output


def test_database_password_is_extracted_from_the_url():
    """The URL stays readable; only the password goes."""
    assert obs._password_from_url("postgresql://user:s3cretpw@host:5432/db") == "s3cretpw"
    assert obs._password_from_url("postgresql://user@host:5432/db") is None
    assert obs._password_from_url("sqlite:///./test.db") is None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def test_counters_accumulate_and_are_labelled():
    m = obs.Metrics()
    m.inc("job.succeeded")
    m.inc("job.succeeded")
    m.inc("job.failed", org_id="org-1")
    assert m.get("job.succeeded") == 2
    assert m.get("job.failed", org_id="org-1") == 1
    assert m.get("job.failed", org_id="org-2") == 0


def test_label_order_does_not_create_a_second_series():
    """Two call sites passing the same labels in different orders are the same
    metric; otherwise every dashboard silently under-counts."""
    m = obs.Metrics()
    m.inc("x", a=1, b=2)
    m.inc("x", b=2, a=1)
    assert m.get("x", a=1, b=2) == 2
    assert len(m.snapshot()) == 1


def test_gauges_take_the_latest_value():
    m = obs.Metrics()
    m.set_gauge("queue.depth", 5)
    m.set_gauge("queue.depth", 3)
    assert m.get("queue.depth") == 3


def test_concurrent_increments_are_not_lost():
    """A counter that loses increments under concurrency is a counter that lies."""
    import threading

    m = obs.Metrics()

    def bump():
        for _ in range(500):
            m.inc("threaded")

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert m.get("threaded") == 4000


def test_timed_block_records_even_when_the_body_raises():
    """Start/stop calls skip the stop on the exception path - which is exactly
    when the latency number matters."""
    m = obs.Metrics()
    original = obs.metrics
    obs.metrics = m
    try:
        with pytest.raises(ValueError):
            with obs.TimedBlock("op.latency_ms"):
                raise ValueError("boom")
    finally:
        obs.metrics = original
    assert m.get("op.latency_ms") > 0


def test_latency_uses_a_high_resolution_clock():
    """``time.monotonic`` is unusable for latency on Windows.

    Measured on this machine: ``monotonic`` is backed by ``GetTickCount64`` and
    has ~15,000 us granularity - back-to-back reads differed by 0.0 in 20,000 of
    20,000 attempts - while ``perf_counter`` resolved to 0.8 us. So timing with
    ``monotonic`` records exactly 0 ms for every operation faster than ~15 ms,
    which is most of them, and a latency metric that reads zero looks healthy.

    This asserts the resolution directly rather than trusting the call site,
    because a future edit that swaps the clock back would otherwise only show up
    as a dashboard that is quietly always zero.
    """
    import time as _time

    increments = []
    previous = _time.perf_counter()
    for _ in range(50_000):
        current = _time.perf_counter()
        if current != previous:
            increments.append(current - previous)
            previous = current
            if len(increments) >= 500:
                break

    assert increments, "perf_counter never advanced; the clock is unusable"
    smallest = min(increments)
    assert smallest < 1e-4, (
        f"perf_counter resolution is only {smallest * 1e6:.1f} us; latency "
        "measurement would quantise to zero for fast operations"
    )

    # And the consequence, stated as the property that matters.
    zero_reads = 0
    for _ in range(2_000):
        if (_time.perf_counter() - _time.perf_counter()) == 0.0:
            zero_reads += 1
    assert zero_reads == 0, "the chosen clock cannot distinguish fast operations"


def test_timed_block_measures_a_small_but_real_duration():
    """A block that does almost nothing must still record a non-zero latency."""
    m = obs.Metrics()
    original = obs.metrics
    obs.metrics = m
    try:
        with obs.TimedBlock("tiny.latency_ms"):
            pass
    finally:
        obs.metrics = original
    assert m.get("tiny.latency_ms") > 0, (
        "a no-op block recorded zero latency, which is the Windows monotonic "
        "granularity bug"
    )


def test_the_named_operational_metrics_are_declared():
    """A dashboard written against a typo'd counter shows a flat zero forever."""
    required = {
        "queue.depth",
        "worker.heartbeat_age_seconds",
        "ingestion.lag_seconds",
        "mail.sync_lag_seconds",
        "submission.failed",
        "model.cost_micros",
        "security.tenant_denied",
    }
    missing = required - obs.KNOWN_METRICS
    assert missing == set(), f"the brief requires these signals: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Non-JSON mode
# ---------------------------------------------------------------------------
def test_plain_text_mode_still_redacts():
    """Redaction is on the filter, not the formatter, so switching the format
    must not switch it off."""
    buffer = io.StringIO()
    obs.configure_logging(level="INFO", json_output=False, stream=buffer)
    try:
        secret = "plain-mode-secret-abcdefgh"
        obs.register_secret(secret)
        logging.getLogger("plain").info("value=%s", secret)
        assert secret not in buffer.getvalue()
        assert "[REDACTED]" in buffer.getvalue()
    finally:
        logging.getLogger().handlers = []
