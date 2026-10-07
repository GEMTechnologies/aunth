"""Durable job ledger: the PostgreSQL side of agent work.

The split this module encodes is the whole point of the design:

* **PostgreSQL is the truth.** Every job, attempt and event is written here,
  inside the same transaction as the state change that caused it.
* **Redis is delivery.** A stream entry can be trimmed, lost or delivered
  twice, and none of those change what happened.

That is why ``enqueue`` writes an ``outbox_events`` row and returns; it never
calls Redis. The relay moves that row to a stream afterwards. Publishing
inline would lose the event whenever the process died between the database
commit and the ``XADD`` - and could publish an event for a change that later
rolled back.

See ADR-0007.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from events.publisher import stream_name

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_ms(started_at: datetime, now: datetime) -> int:
    """Milliseconds between two timestamps, tolerating mixed awareness.

    ``DateTime(timezone=True)`` hands back an aware value from PostgreSQL but a
    naive one from SQLite, so a timestamp that has made a round trip through the
    database can silently differ in awareness from a fresh ``now()``. Comparing
    the two directly raises ``TypeError``. SQLite is not a toy here - it is what
    the test suite and local development run on, and a ledger that cannot record
    an outcome on SQLite is a ledger whose timing metrics are untested.
    """
    if started_at is None:
        return 0
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return int((now - started_at).total_seconds() * 1000)


def _uuid() -> str:
    return str(uuid.uuid4())


class LedgerError(RuntimeError):
    """Raised when the ledger refuses an operation."""


def backoff_delay(attempt: int, base_seconds: int = 5, cap_seconds: int = 3600) -> int:
    """Exponential backoff with a hard cap.

    ``attempt`` is 1-based, so the first retry waits ``base`` and each
    subsequent one doubles. The cap matters more than the curve: an uncapped
    delay eventually exceeds every scheduler window and the job silently
    becomes unschedulable.
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    return min(cap_seconds, base_seconds * (2 ** (attempt - 1)))


class JobLedger:
    """Reads and writes the durable record of agent work."""

    def __init__(self, db: Session, trace_id: Optional[str] = None) -> None:
        self.db = db
        self.trace_id = trace_id

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------
    def enqueue(
        self,
        *,
        org_id: Optional[str],
        job_type: str,
        payload: Optional[dict[str, Any]] = None,
        domain: Optional[str] = None,
        action: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        max_attempts: int = 5,
        available_at: Optional[datetime] = None,
    ) -> tuple[models.Job, bool]:
        """Stage one unit of work.

        Returns ``(job, created)``. ``created is False`` means this exact work
        was already staged under the same idempotency key, and the caller must
        not perform it again - that is the entire mechanism by which a
        redelivered message cannot become a duplicate submission.

        Nothing is published here. The outbox row and the job row commit
        together; the relay delivers afterwards.
        """
        # Default shape is one stream per job type under the `jobs` domain, so
        # a consumer subscribes to exactly the work it handles. Domain/action
        # stay overridable for domain events that are not job-shaped.
        stream = stream_name(domain or "jobs", action or job_type)

        if idempotency_key:
            existing = self.db.execute(
                select(models.Job).where(
                    models.Job.org_id == org_id,
                    models.Job.job_type == job_type,
                    models.Job.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                logger.info(
                    "job.enqueue.deduplicated",
                    extra={"job_id": existing.id, "job_type": job_type},
                )
                return existing, False

        now = _now()
        job = models.Job(
            id=_uuid(),
            org_id=org_id,
            stream=stream,
            job_type=job_type,
            idempotency_key=idempotency_key,
            payload=payload or {},
            state=models.Job.QUEUED,
            attempt=0,
            max_attempts=max_attempts,
            available_at=available_at or now,
            trace_id=self.trace_id,
            created_at=now,
            updated_at=now,
        )
        self.db.add(job)

        # Same transaction, same fate. If this commits, the work is durably
        # recorded even if Redis is down for the next hour.
        self.stage_event(
            org_id=org_id,
            stream=stream,
            event_type="job.enqueued",
            payload={"job_id": job.id, "job_type": job_type},
        )
        self.db.flush()
        return job, True

    def stage_event(
        self,
        *,
        org_id: Optional[str],
        stream: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> models.OutboxEvent:
        """Add an outbox row to the current transaction.

        Call this in the same transaction as the change it describes. If the
        change rolls back, the event does too - which is the entire reason the
        outbox exists rather than publishing inline.
        """
        event = models.OutboxEvent(
            id=_uuid(),
            org_id=org_id,
            stream=stream,
            event_type=event_type,
            payload=payload,
            trace_id=self.trace_id,
            created_at=_now(),
            attempts=0,
        )
        self.db.add(event)
        return event

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def claim(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_seconds: int = 300,
        org_id: Optional[str] = None,
    ) -> Optional[models.JobAttempt]:
        """Take a lease on a job and open an attempt row.

        Returns ``None`` when the job is not claimable - already terminal, or
        leased by a live worker. The check and the write are one UPDATE with a
        state predicate, so two workers racing for the same job cannot both
        win; the loser sees zero rows updated.
        """
        now = _now()
        claimable = (
            models.Job.id == job_id,
            models.Job.state == models.Job.QUEUED,
            models.Job.available_at <= now,
        )
        if org_id is not None:
            claimable += (models.Job.org_id == org_id,)

        result = self.db.execute(
            select(models.Job)
            .where(*claimable)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if result is None:
            return None

        result.state = models.Job.RUNNING
        result.attempt += 1
        result.lease_owner = worker_id
        result.lease_expires_at = now + timedelta(seconds=lease_seconds)
        result.started_at = result.started_at or now
        result.updated_at = now

        attempt = models.JobAttempt(
            id=_uuid(),
            job_id=result.id,
            attempt=result.attempt,
            worker_id=worker_id,
            started_at=now,
        )
        self.db.add(attempt)
        self.db.flush()
        return attempt

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int = 300) -> bool:
        """Extend a lease held by this worker.

        The worker-id predicate matters: a worker that was declared dead and
        had its job reclaimed must not be able to extend the *new* holder's
        lease, or the recovery sweep would never be able to take it back.
        """
        now = _now()
        result = self.db.execute(
            models.Job.__table__.update()
            .where(
                models.Job.id == job_id,
                models.Job.lease_owner == worker_id,
                models.Job.state == models.Job.RUNNING,
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
        )
        return result.rowcount == 1

    def release(self, job_id: str) -> None:
        """Drop a lease without judging the work.

        Used when a worker shuts down cleanly. The job returns to QUEUED and is
        retried; because ``attempt`` already advanced, a job that is released
        repeatedly still walks toward its attempt ceiling rather than looping
        forever.
        """
        now = _now()
        self.db.execute(
            models.Job.__table__.update()
            .where(models.Job.id == job_id, models.Job.state == models.Job.RUNNING)
            .values(
                state=models.Job.QUEUED,
                lease_owner=None,
                lease_expires_at=None,
                available_at=now,
                updated_at=now,
            )
        )

    # ------------------------------------------------------------------
    # Outcomes
    # ------------------------------------------------------------------
    def succeed(self, attempt: models.JobAttempt, *, output: Optional[dict] = None) -> None:
        now = _now()
        attempt.finished_at = now
        attempt.outcome = "SUCCEEDED"
        attempt.duration_ms = _elapsed_ms(attempt.started_at, now)

        job = self._job(attempt)
        job.state = models.Job.SUCCEEDED
        job.finished_at = now
        job.updated_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        if output:
            job.payload = {**(job.payload or {}), "output": output}
        self.db.flush()

    def fail(
        self,
        attempt: models.JobAttempt,
        *,
        category: str,
        error: str,
        retryable: bool = True,
        base_seconds: int = 5,
    ) -> str:
        """Record a failure and decide the next state.

        Returns the resulting job state. Classification is by the explicit
        ``category`` the caller supplies, never by matching the exception
        text - string matching on exception messages breaks the moment
        upstream rewords one, and silently changes retry behaviour.
        """
        now = _now()
        job = self._job(attempt)

        attempt.finished_at = now
        attempt.duration_ms = _elapsed_ms(attempt.started_at, now)
        attempt.outcome = "FAILED"
        attempt.failure_category = category
        attempt.error = error

        job.last_error = error
        job.failure_category = category
        job.updated_at = now
        job.lease_owner = None
        job.lease_expires_at = None

        if not retryable or job.attempt >= job.max_attempts:
            job.state = models.Job.DEAD_LETTER
            job.finished_at = now
            logger.warning(
                "job.dead_lettered",
                extra={
                    "job_id": job.id,
                    "job_type": job.job_type,
                    "attempt": job.attempt,
                    "max_attempts": job.max_attempts,
                    "category": category,
                },
            )
        else:
            job.state = models.Job.QUEUED
            job.available_at = now + timedelta(seconds=backoff_delay(job.attempt, base_seconds))

        self.db.flush()
        return job.state

    def _job(self, attempt: models.JobAttempt) -> models.Job:
        job = self.db.get(models.Job, attempt.job_id)
        if job is None:  # pragma: no cover - FK makes this unreachable
            raise LedgerError(f"attempt {attempt.id} references a missing job")
        return job

    # ------------------------------------------------------------------
    # Recovery and inspection
    # ------------------------------------------------------------------
    def reclaim_expired(self, *, limit: int = 100) -> list[str]:
        """Return jobs whose worker lease expired to the queue.

        This is the database half of stuck-job recovery; the Redis half is
        ``XAUTOCLAIM``. Both are needed: Redis knows what is pending on a
        stream, PostgreSQL knows whether the work was ever recorded.
        """
        now = _now()
        rows = self.db.execute(
            select(models.Job.id)
            .where(
                models.Job.state == models.Job.RUNNING,
                models.Job.lease_expires_at.isnot(None),
                models.Job.lease_expires_at < now,
            )
            .limit(limit)
        ).scalars().all()

        reclaimed: list[str] = []
        for job_id in rows:
            job = self.db.get(models.Job, job_id)
            if job is None or job.state != models.Job.RUNNING:
                continue
            # Already at the ceiling: retrying forever is worse than surfacing
            # it for a human.
            if job.attempt >= job.max_attempts:
                job.state = models.Job.DEAD_LETTER
                job.finished_at = now
                job.last_error = "lease expired and attempt ceiling reached"
                job.updated_at = now
            else:
                job.state = models.Job.QUEUED
                job.lease_owner = None
                job.lease_expires_at = None
                job.available_at = now
                job.updated_at = now
            reclaimed.append(job_id)

        self.db.flush()
        if reclaimed:
            logger.warning("jobs.reclaimed", extra={"count": len(reclaimed)})
        return reclaimed

    def get(self, job_id: str) -> Optional[models.Job]:
        return self.db.get(models.Job, job_id)

    def attempts(self, job_id: str) -> Iterable[models.JobAttempt]:
        return self.db.execute(
            select(models.JobAttempt)
            .where(models.JobAttempt.job_id == job_id)
            .order_by(models.JobAttempt.attempt)
        ).scalars().all()