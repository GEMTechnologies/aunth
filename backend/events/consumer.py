"""Redis Streams consumer with leases, heartbeats and stuck-job recovery.

Delivery semantics
------------------
At-least-once, never exactly-once. ``XACK`` happens only after the ledger has
recorded a terminal outcome, so a worker that dies mid-handler redelivers its
entry. Correctness therefore lives in the database, not in the ack:

* the ledger's ``(org_id, job_type, idempotency_key)`` unique constraint
  collapses duplicate *requests*;
* the ``claim`` UPDATE predicate collapses duplicate *deliveries* - only one
  worker can move a QUEUED job to RUNNING.

Two independent recovery paths, because the two stores fail differently:

* ``XAUTOCLAIM`` reclaims a stream entry abandoned by a dead consumer.
  It only sees entries already in the group's Pending Entries List - an entry
  that was never delivered to anyone is not pending and cannot be claimed.
* ``JobLedger.reclaim_expired`` returns a job whose *database* lease expired.
  This catches the case Redis cannot: a worker that finished its Redis work
  and then lost the database connection, leaving the job RUNNING forever.

Leases, not locks
-----------------
A lock held only in Redis dies with the connection that took it. A lease with
an expiry is recoverable by anyone, so a crashed worker leaks nothing.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from sqlalchemy.orm import Session

import models
import tenant_context
from events.ledger import JobLedger, LedgerError

logger = logging.getLogger(__name__)


def worker_identity(prefix: str = "worker") -> str:
    """A worker id that survives a restart being distinguishable from it.

    The host and pid matter: when a job is stuck, "which machine, which
    process" is the first question asked, and a bare uuid answers neither.
    """
    return f"{prefix}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


# Errors that mean "this will never work"; retrying them only burns attempts
# and delays the human who needs to know. Kept as a mapping to the
# publisher's canonical categories so the DLQ reason is comparable across
# every job type.
NON_RETRYABLE = {
    "VALIDATION_ERROR",
    "POLICY_BLOCKED",
    "PERMANENT_REJECTION",
    "AUTH_EXPIRED",
}


def classify(exc: BaseException) -> tuple[str, str, bool]:
    """Map an exception to ``(category, message, retryable)``.

    Classification is by type and by an explicit marker, never by matching
    the exception text: upstream libraries reword messages between releases,
    and a substring match that silently stops matching changes retry
    behaviour without failing anything.
    """
    from events.publisher import EventPublisherError

    if isinstance(exc, PermanentFailure):
        return "PERMANENT_REJECTION", str(exc), False
    if isinstance(exc, PolicyBlocked):
        return "POLICY_BLOCKED", str(exc), False
    if isinstance(exc, EventPublisherError):
        return "TRANSIENT_NETWORK", str(exc), True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "TRANSIENT_NETWORK", f"{type(exc).__name__}: {exc}", True
    return "TRANSIENT_NETWORK", f"{type(exc).__name__}: {exc}", True


class PermanentFailure(RuntimeError):
    """The work is wrong, not the infrastructure. Do not retry."""


class PolicyBlocked(RuntimeError):
    """Autonomy policy forbids this action. Retry would only repeat the refusal."""


Handler = Callable[[models.Job, JobLedger], Optional[dict[str, Any]]]


class StreamWorker:
    """Consumes one or more job streams and applies handlers."""

    def __init__(
        self,
        db_factory: Callable[[], Session],
        redis_client: Any,
        *,
        worker_id: Optional[str] = None,
        lease_seconds: int = 300,
        block_ms: int = 5000,
        batch_size: int = 10,
    ) -> None:
        self.db_factory = db_factory
        self.redis = redis_client
        self.worker_id = worker_id or worker_identity()
        self.lease_seconds = lease_seconds
        self.block_ms = block_ms
        self.batch_size = batch_size
        self.handlers: dict[str, Handler] = {}

    def handles(self, job_type: str, handler: Handler) -> None:
        """Register the handler for a job type."""
        self.handlers[job_type] = handler

    # ------------------------------------------------------------------
    # Group management
    # ------------------------------------------------------------------
    def ensure_group(self, stream: str, group: str = "workers") -> None:
        """Create the consumer group if absent.

        ``mkstream=True`` matters: a stream that has never been written to
        does not exist, and ``XGROUP CREATE`` against a missing key fails
        unless the stream is created at the same time.
        """
        try:
            self.redis.xgroup_create(stream, group, id="0-0", mkstream=True)
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" not in str(exc):
                raise
            logger.debug("group.exists", extra={"stream": stream, "group": group})

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------
    def poll(self, streams: Iterable[str], group: str = "workers") -> list[tuple[str, dict]]:
        """Read newly delivered entries. Returns ``(stream, fields)`` pairs."""
        received: list[tuple[str, dict]] = []
        streams = list(streams)
        if not streams:
            return received
        try:
            results = self.redis.xreadgroup(
                group, self.worker_id, streams={s: ">" for s in streams},
                count=self.batch_size, block=self.block_ms,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("worker.poll.failed", extra={"error": str(exc)})
            return received

        for stream, entries in results or []:
            for entry_id, fields in entries:
                received.append((stream, {**fields, "_entry_id": entry_id, "_stream": stream}))
        return received

    def reclaim(self, streams: Iterable[str], group: str = "workers",
                min_idle_ms: int = 60_000) -> list[tuple[str, dict]]:
        """Reclaim entries abandoned by a dead consumer.

        Only entries already delivered to this group and left unacked are
        visible; ``start='0-0'`` is required, and omitting it is an error
        rather than a default.
        """
        reclaimed: list[tuple[str, dict]] = []
        for stream in streams:
            try:
                result = self.redis.xautoclaim(
                    stream, group, self.worker_id, min_idle_ms, "0-0",
                    count=self.batch_size,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("worker.reclaim.failed", extra={"stream": stream, "error": str(exc)})
                continue

            # Redis 7 returns [next_start, entries, deleted]; older builds
            # return only entries. Normalise rather than assume.
            entries = result[1] if isinstance(result, (list, tuple)) else result
            for entry_id, fields in entries or []:
                if isinstance(fields, (list, tuple)):
                    fields = dict(fields)
                reclaimed.append((stream, {**(fields or {}), "_entry_id": entry_id, "_stream": stream}))
        if reclaimed:
            logger.warning("worker.reclaimed", extra={"count": len(reclaimed)})
        return reclaimed

    def ack(self, stream: str, entry_id: str, group: str = "workers") -> bool:
        try:
            return bool(self.redis.xack(stream, group, entry_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("worker.ack.failed", extra={"stream": stream, "entry": entry_id, "error": str(exc)})
            return False

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    @staticmethod
    def _job_id_from(fields: dict) -> Optional[str]:
        """Pull the job id out of a delivered entry.

        Two shapes reach a worker: a job envelope, which carries ``job_id``
        directly, and an outbox event, which nests a JSON payload. Accepting
        both matters because the relay publishes the latter.
        """
        direct = fields.get("job_id")
        if direct:
            return direct
        raw = fields.get("payload")
        if not raw:
            return None
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return parsed.get("job_id")
        return None

    def process(self, stream: str, fields: dict, group: str = "workers") -> str:
        """Handle one delivered entry and record the outcome.

        Returns the resulting job state. The Redis ack is deliberately last:
        anything that fails before it leaves the entry pending, and
        ``XAUTOCLAIM`` will bring it back.
        """
        entry_id = fields.get("_entry_id")
        job_id = self._job_id_from(fields)

        if not job_id:
            # A stream entry this worker does not own. Not an error: it is
            # another worker's job, or a plain domain event nobody subscribed to.
            self.ack(stream, entry_id, group)
            return "SKIPPED"

        db = self.db_factory()
        try:
            job = self._claim(db, job_id, fields)
            if isinstance(job, str):
                self.ack(stream, entry_id, group)
                return job

            job_row, org_id, attempt_no = job
            handler = self.handlers[job_row.job_type]
            logger.info(
                "worker.job.started",
                extra={"job_id": job_row.id, "job_type": job_row.job_type, "attempt": attempt_no},
            )

            try:
                output = handler(job_row, JobLedger(db))
            except Exception as exc:  # noqa: BLE001 - classified, not guessed
                category, message, retryable = classify(exc)
                db.rollback()
                state = self._record_failure(db, job_row.id, attempt_no, category, message, retryable)
                self.ack(stream, entry_id, group)
                return state

            db.rollback()
            self._record_success(db, job_row.id, attempt_no, output)
            self.ack(stream, entry_id, group)
            return models.Job.SUCCEEDED
        finally:
            try:
                tenant_context.clear_tenant(db)
            except Exception:  # noqa: BLE001 - best effort on a torn-down session
                pass
            db.close()

    def _claim(self, db: Session, job_id: str, fields: dict):
        """Claim the job under its own tenant.

        Returns ``(job, org_id, attempt_no)`` on success, or a state string
        when there is nothing to do.

        The tenant comes from the ledger row, never from the message. A
        producer that lied about ``org_id`` would otherwise be able to aim a
        worker at another tenant's data.
        """
        ledger = JobLedger(db, trace_id=fields.get("trace_id") or None)
        job = ledger.get(job_id)
        if job is None:
            # A stream entry can outlive its job row. Retrying cannot recreate
            # one, so ack and drop rather than poison the stream.
            logger.warning("worker.job_missing", extra={"job_id": job_id})
            return "MISSING"

        if job.job_type not in self.handlers:
            logger.warning("worker.no_handler", extra={"job_type": job.job_type})
            return "UNHANDLED"

        org_id = job.org_id
        tenant_context.set_tenant(db, org_id, None)

        attempt = ledger.claim(
            job_id=job.id, worker_id=self.worker_id,
            lease_seconds=self.lease_seconds, org_id=org_id,
        )
        if attempt is None:
            # Already terminal, or another worker holds the lease. This is the
            # duplicate-delivery guard: exactly one worker can win.
            db.rollback()
            return "NOT_CLAIMABLE"

        attempt_no = attempt.attempt
        db.commit()
        return job, org_id, attempt_no

    def _open(self, db: Session, job_id: str, org_id: Optional[str], attempt_no: int):
        """Re-open a transaction for the job's tenant and return its attempt."""
        tenant_context.set_tenant(db, org_id, None)
        ledger = JobLedger(db)
        for attempt in ledger.attempts(job_id):
            if attempt.attempt == attempt_no:
                return ledger, attempt
        raise LedgerError(f"job {job_id} has no attempt {attempt_no}")

    def _record_success(self, db: Session, job_id: str, attempt_no: int, output: Optional[dict]) -> None:
        org_id = db.get(models.Job, job_id).org_id
        ledger, attempt = self._open(db, job_id, org_id, attempt_no)
        ledger.succeed(attempt, output=output)
        db.commit()

    def _record_failure(
        self, db: Session, job_id: str, attempt_no: int,
        category: str, message: str, retryable: bool,
    ) -> str:
        org_id = db.get(models.Job, job_id).org_id
        ledger, attempt = self._open(db, job_id, org_id, attempt_no)
        state = ledger.fail(attempt, category=category, error=message, retryable=retryable)
        db.commit()
        return state

    # ------------------------------------------------------------------
    # One iteration
    # ------------------------------------------------------------------
    def run_once(self, streams: Iterable[str], group: str = "workers") -> int:
        """Drain what's waiting, then reclaim what was abandoned.

        New work is handled before reclaimed work so a backlog does not
        starve a job whose worker died an hour ago.
        """
        streams = list(streams)
        for stream in streams:
            self.ensure_group(stream, group)

        handled = 0
        for stream, fields in self.poll(streams, group):
            self.process(stream, fields, group)
            handled += 1
        for stream, fields in self.reclaim(streams, group):
            self.process(stream, fields, group)
            handled += 1

        self.recover_expired_leases()
        return handled

    def recover_expired_leases(self) -> list[str]:
        """Return database jobs whose worker lease expired to the queue."""
        db = self.db_factory()
        try:
            # Cross-tenant by nature: it must find every stuck job regardless of
            # tenant. Returns ids only - no payload is exposed - so the sweep
            # cannot become a read oracle for tenant data.
            reclaimed = JobLedger(db).reclaim_expired()
            db.commit()
            return reclaimed
        except Exception as exc:  # noqa: BLE001
            logger.warning("worker.recovery.failed", extra={"error": str(exc)})
            db.rollback()
            return []
        finally:
            db.close()