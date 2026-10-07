"""Agent runtime: job ledger, outbox relay, stream consumer.

These cover three of the named critical tests from the build brief -
"workflow resume after crash", "duplicate job delivery" and "Redis event
idempotency" - plus the retry/backoff/DLQ and lease semantics they depend on.

Every mechanism here was proven load-bearing by inversion; the inversions are
recorded in the commit message for this file, because a test that cannot fail
under the mutation it targets is decoration.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from events.ledger import JobLedger, backoff_delay  # noqa: E402
from events.relay import Inbox, OutboxRelay  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path):
    # Schema copied from a session template rather than rebuilt: create_all to a
    # file on this filesystem costs ~3.8s per test because the schema has 38 tables
    # and 203 indexes. See tests/conftest.py::make_sqlite_db.
    engine, session = make_sqlite_db(tmp_path, "runtime.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def user(db):
    row = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(row)
    db.commit()
    return row.id


@pytest.fixture
def org(db, user):
    return _make_org(db, user, "test-ngo")


def _make_org(db, owner_user_id, slug):
    """Real rows, not bare UUIDs.

    ``jobs.org_id`` is a foreign key, so a made-up tenant id would be rejected
    by the database rather than testing what the tests claim to test.
    """
    row = models.Organisation(
        id=str(uuid.uuid4()), name="Test NGO", slug=slug, owner_user_id=owner_user_id
    )
    db.add(row)
    db.commit()
    return row.id


def _ledger(db, trace_id=None):
    return JobLedger(db, trace_id=trace_id)


def _utc(value):
    """Compare like with like.

    SQLite hands back naive datetimes for ``DateTime(timezone=True)`` columns
    while PostgreSQL hands back aware ones, so a test that compares a
    round-tripped column against ``datetime.now(timezone.utc)`` passes on one
    backend and raises ``TypeError`` on the other. Normalising here keeps the
    assertion meaningful on both.
    """
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_same_idempotency_key_stages_work_once(db, org):
    """The first line of defence against submitting an application twice."""
    first, created_first = _ledger(db).enqueue(
        org_id=org, job_type="submission", payload={"a": 1}, idempotency_key="submit-opp-9"
    )
    db.commit()
    second, created_second = _ledger(db).enqueue(
        org_id=org, job_type="submission", payload={"a": 1}, idempotency_key="submit-opp-9"
    )
    db.commit()

    assert created_first is True
    assert created_second is False, "a redelivered request staged a second job"
    assert second.id == first.id
    assert len(list(db.execute(select(models.Job)))) == 1


def test_null_idempotency_keys_never_collide(db, org):
    """Repeatable work leaves the key NULL.

    NULLs are distinct in a unique constraint in both PostgreSQL and SQLite, so
    "no key" means "always allowed" rather than "collides with every other job
    that also has no key". Proven rather than assumed: both rows must exist.
    """
    a, _ = _ledger(db).enqueue(org_id=org, job_type="poll", payload={}, idempotency_key=None)
    b, _ = _ledger(db).enqueue(org_id=org, job_type="poll", payload={}, idempotency_key=None)
    db.commit()
    assert a.id != b.id
    assert len(list(db.execute(select(models.Job)))) == 2


def test_idempotency_is_scoped_per_tenant(db, user):
    """Two tenants staging the same key are two different pieces of work.

    A globally unique key would make tenant B's job silently dedupe into
    tenant A's - a cross-tenant information leak via a job row.
    """
    org_a = _make_org(db, user, "tenant-a")
    org_b = _make_org(db, user, "tenant-b")

    a, created_a = _ledger(db).enqueue(
        org_id=org_a, job_type="x", payload={}, idempotency_key="same"
    )
    b, created_b = _ledger(db).enqueue(
        org_id=org_b, job_type="x", payload={}, idempotency_key="same"
    )
    db.commit()
    assert created_a and created_b
    assert a.id != b.id


def test_different_job_types_do_not_share_a_key(db, org):
    _, created = _ledger(db).enqueue(
        org_id=org, job_type="draft_writer", payload={}, idempotency_key="shared"
    )
    db.commit()
    _, created2 = _ledger(db).enqueue(
        org_id=org, job_type="budget", payload={}, idempotency_key="shared"
    )
    db.commit()
    assert created and created2


# ---------------------------------------------------------------------------
# Transactional outbox
# ---------------------------------------------------------------------------
def test_enqueue_and_outbox_share_one_transaction(db, org):
    """The state change and the record that it happened are one atomic fact."""
    _ledger(db).enqueue(org_id=org, job_type="research", payload={}, idempotency_key="k")
    db.commit()
    assert db.execute(select(models.OutboxEvent)).scalars().all()


def test_rollback_leaves_neither_a_job_nor_an_event(db, org):
    """Publishing inline would have delivered work that never committed."""
    _ledger(db).enqueue(org_id=org, job_type="research", payload={}, idempotency_key="k")
    db.rollback()
    assert db.execute(select(models.Job)).scalars().all() == []
    assert db.execute(select(models.OutboxEvent)).scalars().all() == []


# ---------------------------------------------------------------------------
# Claiming and leases
# ---------------------------------------------------------------------------
def test_only_one_worker_can_claim_a_job(db, org):
    """Duplicate delivery is stopped by a state predicate, not by luck."""
    job, _ = _ledger(db).enqueue(
        org_id=org, job_type="research", payload={}, idempotency_key="k"
    )
    db.commit()

    first = _ledger(db).claim(job_id=job.id, worker_id="worker-a")
    db.commit()
    assert first is not None

    second = _ledger(db).claim(job_id=job.id, worker_id="worker-b")
    db.commit()
    assert second is None, "two workers held the same job"


def test_claim_is_refused_while_the_lease_is_live(db, org):
    job, _ = _ledger(db).enqueue(org_id=org, job_type="r", payload={}, idempotency_key="k")
    db.commit()
    a = _ledger(db).claim(job_id=job.id, worker_id="w1", lease_seconds=300)
    db.commit()
    assert a is not None
    assert _ledger(db).claim(job_id=job.id, worker_id="w2", lease_seconds=300) is None


def test_heartbeat_only_works_for_the_lease_holder(db, org):
    """A worker declared dead must not extend its replacement's lease.

    Without the worker-id predicate the recovery sweep could never take the
    job back: the dead worker would keep renewing it.
    """
    job, _ = _ledger(db).enqueue(org_id=org, job_type="r", payload={}, idempotency_key="k")
    db.commit()
    _ledger(db).claim(job_id=job.id, worker_id="w1")
    db.commit()

    assert _ledger(db).heartbeat(job.id, "w2") is False
    assert _ledger(db).heartbeat(job.id, "w1") is True


def test_job_in_the_future_is_not_claimable(db, org):
    """Backoff must actually delay the next attempt."""
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    job, _ = _ledger(db).enqueue(
        org_id=org, job_type="r", payload={}, idempotency_key="k", available_at=later
    )
    db.commit()
    assert _ledger(db).claim(job_id=job.id, worker_id="w1") is None


def test_claim_scoped_to_the_wrong_tenant_finds_nothing(db, org):
    """The tenant predicate is an authorisation input, not decoration."""
    job, _ = _ledger(db).enqueue(org_id=org, job_type="r", payload={}, idempotency_key="k")
    db.commit()

    assert _ledger(db).claim(job_id=job.id, worker_id="w1", org_id="some-other-tenant") is None


# ---------------------------------------------------------------------------
# Retry, backoff, dead letter
# ---------------------------------------------------------------------------
def test_backoff_grows_and_is_capped():
    assert backoff_delay(1, base_seconds=5) == 5
    assert backoff_delay(2, base_seconds=5) == 10
    assert backoff_delay(3, base_seconds=5) == 20
    assert backoff_delay(50, base_seconds=5) == 3600
    with pytest.raises(ValueError):
        backoff_delay(0)


def test_failure_requeues_with_a_delay(db, org):
    job, _ = _ledger(db).enqueue(
        org_id=org, job_type="r", payload={}, idempotency_key="k", max_attempts=3
    )
    db.commit()
    attempt = _ledger(db).claim(job_id=job.id, worker_id="w1")
    db.commit()

    state = _ledger(db).fail(attempt, category="TRANSIENT_NETWORK", error="boom", base_seconds=5)
    db.commit()

    assert state == models.Job.QUEUED
    fresh = _ledger(db).get(job.id)
    assert fresh.failure_category == "TRANSIENT_NETWORK"
    assert _utc(fresh.available_at) > datetime.now(timezone.utc)
    assert _ledger(db).claim(job_id=job.id, worker_id="w2") is None, "backoff did not delay"


def test_retryable_failure_does_not_dead_letter_before_the_ceiling(db, org):
    job, _ = _ledger(db).enqueue(
        org_id=org, job_type="r", payload={}, idempotency_key="k", max_attempts=3
    )
    db.commit()
    for _ in range(2):
        attempt = _ledger(db).claim(job_id=job.id, worker_id="w1")
        db.commit()
        assert attempt is not None
        _ledger(db).fail(attempt, category="TRANSIENT_NETWORK", error="boom")
        db.commit()
        # Clear the backoff so the next attempt is claimable immediately.
        _ledger(db).db.execute(
            models.Job.__table__.update()
            .where(models.Job.id == job.id)
            .values(available_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        db.commit()
    assert _ledger(db).get(job.id).state == models.Job.QUEUED


def test_non_retryable_failure_dead_letters_immediately(db, org):
    """A policy refusal must not be retried five times."""
    job, _ = _ledger(db).enqueue(
        org_id=org, job_type="r", payload={}, idempotency_key="k", max_attempts=5
    )
    db.commit()
    attempt = _ledger(db).claim(job_id=job.id, worker_id="w1")
    db.commit()

    state = _ledger(db).fail(
        attempt, category="POLICY_BLOCKED", error="autonomy forbids this", retryable=False
    )
    db.commit()
    assert state == models.Job.DEAD_LETTER


def test_exhausting_attempts_dead_letters(db, org):
    job, _ = _ledger(db).enqueue(
        org_id=org, job_type="r", payload={}, idempotency_key="k", max_attempts=1
    )
    db.commit()
    attempt = _ledger(db).claim(job_id=job.id, worker_id="w1")
    db.commit()
    state = _ledger(db).fail(attempt, category="TRANSIENT_NETWORK", error="boom")
    db.commit()
    assert state == models.Job.DEAD_LETTER
    assert _ledger(db).claim(job_id=job.id, worker_id="w2") is None


def test_attempt_history_survives_for_investigation(db, org):
    """The dead-letter queue is only useful if the whole story is in it."""
    job, _ = _ledger(db).enqueue(
        org_id=org, job_type="r", payload={}, idempotency_key="k", max_attempts=2
    )
    db.commit()
    for _ in range(2):
        attempt = _ledger(db).claim(job_id=job.id, worker_id="w1")
        db.commit()
        _ledger(db).fail(attempt, category="TRANSIENT_NETWORK", error=f"boom-{_}")
        db.commit()
        _ledger(db).db.execute(
            models.Job.__table__.update()
            .where(models.Job.id == job.id)
            .values(available_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        db.commit()

    history = _ledger(db).attempts(job.id)
    assert [a.attempt for a in history] == [1, 2]
    assert all(a.outcome == "FAILED" for a in history)
    assert [a.error for a in history] == ["boom-0", "boom-1"]
    assert all(a.duration_ms is not None for a in history)


def test_success_records_output_and_clears_the_lease(db, org):
    job, _ = _ledger(db).enqueue(org_id=org, job_type="r", payload={}, idempotency_key="k")
    db.commit()
    attempt = _ledger(db).claim(job_id=job.id, worker_id="w1")
    db.commit()
    _ledger(db).succeed(attempt, output={"documents": 3})
    db.commit()

    fresh = _ledger(db).get(job.id)
    assert fresh.state == models.Job.SUCCEEDED
    assert fresh.lease_owner is None
    assert fresh.payload["output"] == {"documents": 3}


def test_deleting_a_job_removes_its_attempts(db, org):
    """A DLQ row with orphaned attempt history is worse than none."""
    job, _ = _ledger(db).enqueue(org_id=org, job_type="r", payload={}, idempotency_key="k")
    db.commit()
    _ledger(db).claim(job_id=job.id, worker_id="w1")
    db.commit()
    db.delete(_ledger(db).get(job.id))
    db.commit()
    assert db.execute(select(models.JobAttempt)).scalars().all() == []


# ---------------------------------------------------------------------------
# Workflow resume after crash
# ---------------------------------------------------------------------------
def test_expired_lease_returns_the_job_to_the_queue(db, org):
    """Simulates a worker killed mid-handler.

    Nothing about this is Redis: the database knows the lease lapsed, and
    recovery has to work even when the message never came back.
    """
    job, _ = _ledger(db).enqueue(org_id=org, job_type="r", payload={}, idempotency_key="k")
    db.commit()
    attempt = _ledger(db).claim(job_id=job.id, worker_id="doomed", lease_seconds=300)
    db.commit()

    # Wind the clock back by rewriting the lease, rather than sleeping.
    _ledger(db).db.execute(
        models.Job.__table__.update()
        .where(models.Job.id == job.id)
        .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    db.commit()

    reclaimed = _ledger(db).reclaim_expired()
    db.commit()
    assert reclaimed == [job.id]
    fresh = _ledger(db).get(job.id)
    assert fresh.state == models.Job.QUEUED
    assert fresh.lease_owner is None
    assert _ledger(db).claim(job_id=job.id, worker_id="replacement") is not None


def test_live_lease_is_not_reclaimed(db, org):
    job, _ = _ledger(db).enqueue(org_id=org, job_type="r", payload={}, idempotency_key="k")
    db.commit()
    _ledger(db).claim(job_id=job.id, worker_id="w1", lease_seconds=600)
    db.commit()
    assert _ledger(db).reclaim_expired() == []


def test_reclaim_at_the_attempt_ceiling_dead_letters(db, org):
    """Looping forever is worse than surfacing the job for a human."""
    job, _ = _ledger(db).enqueue(
        org_id=org, job_type="r", payload={}, idempotency_key="k", max_attempts=1
    )
    db.commit()
    _ledger(db).claim(job_id=job.id, worker_id="w1")
    db.commit()
    _ledger(db).db.execute(
        models.Job.__table__.update()
        .where(models.Job.id == job.id)
        .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    db.commit()

    _ledger(db).reclaim_expired()
    db.commit()
    assert _ledger(db).get(job.id).state == models.Job.DEAD_LETTER


def test_release_returns_the_job_without_judging_it(db, org):
    job, _ = _ledger(db).enqueue(org_id=org, job_type="r", payload={}, idempotency_key="k")
    db.commit()
    _ledger(db).claim(job_id=job.id, worker_id="w1")
    db.commit()
    _ledger(db).release(job.id)
    db.commit()

    fresh = _ledger(db).get(job.id)
    assert fresh.state == models.Job.QUEUED
    assert fresh.lease_owner is None
    assert _ledger(db).claim(job_id=job.id, worker_id="w2") is not None


# ---------------------------------------------------------------------------
# Inbound idempotency
# ---------------------------------------------------------------------------
def test_duplicate_webhook_is_claimed_once(db):
    """Gmail retries a webhook for days; the second delivery must be inert."""
    inbox = Inbox(db)
    first, is_new = inbox.claim(source="gmail", external_event_id="msg-123", payload={"a": 1})
    db.commit()
    assert is_new is True

    second, is_new_again = inbox.claim(source="gmail", external_event_id="msg-123", payload={"a": 1})
    db.commit()
    assert is_new_again is False
    assert second.id == first.id
    assert len(db.execute(select(models.InboxEvent)).scalars().all()) == 1


def test_the_same_id_from_a_different_provider_is_a_different_event(db):
    inbox = Inbox(db)
    inbox.claim(source="gmail", external_event_id="shared-id", payload={})
    db.commit()
    _, is_new = inbox.claim(source="m365", external_event_id="shared-id", payload={})
    db.commit()
    assert is_new is True


def test_inbox_marks_processed(db):
    inbox = Inbox(db)
    event, _ = inbox.claim(source="gmail", external_event_id="m1", payload={})
    db.commit()
    inbox.mark(event, status=models.InboxEvent.PROCESSED, note="correlated to application")
    db.commit()
    assert event.processed_at is not None


# ---------------------------------------------------------------------------
# The inbound race
# ---------------------------------------------------------------------------
def test_the_database_rejects_a_duplicate_webhook_row(db):
    """The backstop behind ``Inbox.claim``.

    ``claim`` checks for an existing row before inserting, so the duplicate is
    normally caught there. That check is inherently racy - two concurrent
    deliveries both see nothing. What makes the race *safe* is this unique
    constraint, so it is asserted directly rather than assumed.
    """
    Inbox(db).claim(source="gmail", external_event_id="dup", payload={})
    db.commit()
    db.add(models.InboxEvent(source="gmail", external_event_id="dup", payload={},
                             received_at=datetime.now(timezone.utc)))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_lost_race_keeps_the_callers_earlier_work(db, monkeypatch):
    """A redelivery must not silently discard what the request already wrote.

    This forces the branch that a single-threaded test can never reach: the
    pre-insert SELECT is stubbed to miss, so the real INSERT goes to the real
    constraint and raises. The recovery path then has to work with the
    caller's pending writes still intact - which a bare ``db.rollback()`` would
    have thrown away.
    """
    Inbox(db).claim(source="gmail", external_event_id="dup", payload={"n": 1})
    db.commit()

    # Work the caller did BEFORE noticing this was a redelivery.
    db.add(models.InboxEvent(source="gmail", external_event_id="other", payload={},
                             received_at=datetime.now(timezone.utc)))
    db.flush()

    real_execute = db.execute
    blinded = {"count": 0}

    def blind_execute(stmt, *a, **kw):
        # Only the FIRST lookup misses, as in a genuine race. Blinding every
        # select would also hide the recovery re-read and prove nothing.
        if "inbox_events" in str(stmt) and blinded["count"] == 0:
            blinded["count"] += 1
            return _EmptyResult()
        return real_execute(stmt, *a, **kw)

    monkeypatch.setattr(db, "execute", blind_execute)
    event, is_new = Inbox(db).claim(source="gmail", external_event_id="dup", payload={"n": 1})
    monkeypatch.undo()

    assert is_new is False
    assert event is not None, "the recovery path lost the existing row"
    assert event.external_event_id == "dup"
    assert blinded["count"] == 1

    db.commit()  # must not raise: the earlier row survived the lost race
    assert db.execute(select(models.InboxEvent)).scalars().all() is not None
    survivors = {e.external_event_id for e in db.execute(select(models.InboxEvent)).scalars()}
    assert survivors == {"dup", "other"}


class _EmptyResult:
    """A result that claims the inbox SELECT found nothing."""

    def scalar_one_or_none(self):
        return None