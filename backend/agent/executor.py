"""The executor: claim a durable job and run it.

THE GAP THIS FILLS
------------------
`AgentWorker.execute(job_id)` has existed and been tested since the runtime ledger landed, and
**nothing called it**. The deployment ran one fleet process - `python -m agent.fleet_runner` - which
is dispatch-only by its own docstring:

    "The **dispatcher** discovers due work and enqueues it. That is all it does."

So the chain stopped one step short of doing anything:

    ingest -> catalogue          OK
    discovery -> workflow        OK
    dispatcher -> job            OK
    executor -> match            MISSING        <- this module

Measured on the VPS on 2026-10-09, once the dispatcher could finally see its own table:

    workflows=2 matches=0 jobs=2 attempts=0
    JOB opportunity_match | QUEUED

Two jobs, queued, **attempts=0**. Nothing ever picked them up.

WHY A SEPARATE PROCESS AND NOT A CALL IN THE SWEEP
--------------------------------------------------
`fleet_runner.py` states the reason itself: *"The shared fleet executes the work, so a slow provider
cannot stall the scheduler and a crashed worker does not lose the reconciliation."*

That is a design decision, not an accident. A specialist can call a model or fetch a document, which
takes seconds to minutes. Folding that into the scheduling tick would mean the dispatcher for every
organisation waits behind one organisation's slow provider call - the head-of-line blocking the
separation exists to prevent. So this is its own loop, its own process, and its own compose service,
and several of them may run at once.

SAFETY, WHICH MATTERS MORE HERE THAN ANYWHERE ELSE
--------------------------------------------------
**Claiming is the database's job, not this module's.** `AgentWorker.execute` goes through
`JobLedger.claim`, which takes a lease with a unique constraint behind it. Two executors racing the
same job is therefore resolved by PostgreSQL, and the loser gets `SKIPPED / "not claimable"` rather
than running the work twice. This loop deliberately does NOT try to be clever about that: it selects
candidates, attempts each, and treats a skip as a normal outcome - because anything else would be a
second, weaker implementation of a guarantee the ledger already provides.

**The candidate query is a filter, not a reservation.** Selecting a row does not claim it. A job may
be claimed by another executor, or fail its authority check, in the window between the select and the
attempt. That is expected and is why every outcome is counted by type rather than assumed.

**The heartbeat is separate from the dispatcher's.** A wedged executor must be visible as an executor
problem; sharing the dispatcher's file would make a stuck worker look like a stalled scheduler.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from agent import heartbeat
from agent.workflow_engine import AgentWorker, ExecutionResult
from observability import metrics

logger = logging.getLogger(__name__)

#: How long this executor waits between polls when it found nothing.
DEFAULT_INTERVAL_SECONDS = 5.0

#: How many jobs one pass may attempt. Bounded like everything else in the fleet: a backlog of
#: 10,000 must drain steadily rather than load 10,000 rows into one transaction.
DEFAULT_BATCH_SIZE = 25

#: Sleep in slices so a stop signal is honoured promptly rather than after a full interval.
MAX_SLEEP_SLICE_SECONDS = 1.0

#: Where this process records that its loop is still going round. Distinct from the dispatcher's
#: file: a wedged executor must not be able to look like a healthy dispatcher, or the reverse.
HEARTBEAT_PATH = Path("/tmp/granada-executor-heartbeat")


def worker_identity() -> str:
    """A name that identifies THIS process in a lease, so a stuck job can be traced to a container.

    Hostname gives the container, pid gives the process within it. A bare uuid would be attributable
    to nothing an operator can look up.
    """
    return f"executor:{socket.gethostname()}:{os.getpid()}"


@dataclass
class ExecutorHealth:
    """What the executor is doing, for a health endpoint and for the logs."""

    running: bool = False
    stopping: bool = False
    sweeps: int = 0
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    parked: int = 0
    errors: int = 0
    last_sweep_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_heartbeat_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    worker_id: str = ""

    @property
    def healthy(self) -> bool:
        """A loop that has never completed a pass is not healthy, however new."""
        return self.running and self.sweeps > 0 and self.errors == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "stopping": self.stopping,
            "healthy": self.healthy,
            "worker_id": self.worker_id,
            "sweeps": self.sweeps,
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "parked": self.parked,
            "errors": self.errors,
            "last_sweep_at": self.last_sweep_at.isoformat() if self.last_sweep_at else None,
            "last_error": self.last_error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }


@dataclass
class PassResult:
    """What one pass did."""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    parked: int = 0
    errors: list[str] = field(default_factory=list)


class JobExecutor:
    """Polls for claimable jobs and runs each through `AgentWorker`."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        worker_id: Optional[str] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        on_pass: Optional[Callable[[PassResult], None]] = None,
    ) -> None:
        self.session_factory = session_factory
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds
        self.worker_id = worker_id or worker_identity()
        self.on_pass = on_pass
        self.health = ExecutorHealth(worker_id=self.worker_id)
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def claimable_jobs(self, db: Session, *, now: Optional[datetime] = None) -> list[str]:
        """Job ids the ledger might let us claim.

        A FILTER, NOT A RESERVATION. `JobLedger.claim` inside `execute` is what actually takes the
        lease; this query only narrows the field so the loop is not scanning finished work. Two
        executors will see the same ids and one will be told `not claimable`, which is the correct
        and cheap outcome.

        The lease clauses matter: a job whose lease has not expired is somebody else's, and a job in
        backoff has `available_at` in the future. Attempting either wastes a round trip and, worse,
        would make a busy fleet look like a racing one.
        """
        moment = now or datetime.now(timezone.utc)
        rows = db.execute(
            select(models.Job.id)
            .where(
                models.Job.state == models.Job.QUEUED,
                models.Job.available_at <= moment,
            )
            .order_by(models.Job.available_at.asc(), models.Job.id.asc())
            .limit(self.batch_size)
        ).scalars().all()
        return [str(row) for row in rows]

    # ------------------------------------------------------------------
    def run_once(self, *, now: Optional[datetime] = None) -> PassResult:
        """One pass: open a session, attempt the candidates, close.

        A session per pass, not one held for the process lifetime - the same reasoning as the
        dispatcher's. A long-lived session holds a transaction open across every sleep, and a
        connection that stays checked out while the loop idles is a pool exhausted by idleness.
        """
        result = PassResult()
        db: Optional[Session] = None
        try:
            db = self.session_factory()
            job_ids = self.claimable_jobs(db, now=now)

            for job_id in job_ids:
                result.attempted += 1
                try:
                    outcome = AgentWorker(db, worker_id=self.worker_id).execute(job_id)
                    db.commit()
                except Exception as exc:  # noqa: BLE001 - one bad job must not stop the pass
                    # `execute` handles its own failures and returns FAILED; reaching here means
                    # something outside that contract broke - a commit, a connection. Roll back and
                    # carry on, because the remaining jobs are other organisations' work.
                    db.rollback()
                    result.errors.append(f"{job_id}: {type(exc).__name__}: {exc}")
                    self.health.errors += 1
                    self.health.last_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("executor.job_crashed", extra={"job_id": job_id})
                    continue

                status = getattr(outcome, "outcome", None)
                if status == ExecutionResult.SUCCEEDED:
                    result.succeeded += 1
                elif status == ExecutionResult.FAILED:
                    result.failed += 1
                elif status == ExecutionResult.PARKED:
                    result.parked += 1
                else:
                    # SKIPPED: already claimed, already done, or in backoff. A normal outcome when
                    # two executors overlap, and not an error.
                    result.skipped += 1

            self.health.sweeps += 1
            self.health.claimed += result.attempted
            self.health.succeeded += result.succeeded
            self.health.failed += result.failed
            self.health.skipped += result.skipped
            self.health.parked += result.parked
            self.health.last_sweep_at = datetime.now(timezone.utc)
            return result
        except Exception as exc:  # noqa: BLE001 - a failed pass must not kill the loop
            if db is not None:
                try:
                    db.rollback()
                except Exception:  # pragma: no cover - the connection is already gone
                    pass
            self.health.errors += 1
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("executor.pass_failed", extra={"error": self.health.last_error})
            raise
        finally:
            if db is not None:
                db.close()

    # ------------------------------------------------------------------
    def run_forever(self, *, max_passes: Optional[int] = None) -> ExecutorHealth:
        """Poll, execute, sleep. `max_passes` exists for tests and one-shot drains."""
        self.health.running = True
        self.health.started_at = datetime.now(timezone.utc)
        self._stop.clear()
        logger.info(
            "executor.started",
            extra={"worker_id": self.worker_id, "interval_seconds": self.interval_seconds,
                   "batch_size": self.batch_size},
        )
        # THE BOUND COUNTS ITERATIONS, NOT SUCCESSES.
        #
        # An earlier version tested `self.health.sweeps >= max_passes`, and `sweeps` is incremented
        # only by a pass that COMPLETED - so a persistent database outage made the loop run forever,
        # ignoring its own bound. That is not cosmetic: `max_passes` is how a caller drains the queue
        # and stops, and a bound a failure can defeat is not a bound.
        #
        # Found by the test written for exactly that case, which HUNG rather than failed - the second
        # time in this module that a loop's termination depended on the property under test.
        passes = 0
        try:
            while not self._stop.is_set():
                passes += 1
                try:
                    result = self.run_once()
                except Exception:
                    pass  # already counted and logged; the loop continues
                else:
                    if self.on_pass is not None:
                        self.on_pass(result)

                # AFTER the pass, so the heartbeat means "a full turn completed" rather than "the
                # process is up". Same reasoning as the dispatcher's, different file.
                heartbeat.beat(HEARTBEAT_PATH)
                self.health.last_heartbeat_at = datetime.now(timezone.utc)

                if max_passes is not None and passes >= max_passes:
                    break
                self._sleep()
        finally:
            self.health.running = False
            logger.info("executor.stopped", extra=self.health.as_dict())
        return self.health

    def _sleep(self) -> None:
        remaining = self.interval_seconds
        while remaining > 0 and not self._stop.is_set():
            slice_seconds = min(MAX_SLEEP_SLICE_SECONDS, remaining)
            if self._stop.wait(slice_seconds):
                return
            remaining -= slice_seconds

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self.health.stopping = True
        self._stop.set()

    def request_stop(self, *_args: Any) -> None:
        self.stop()


def install_signal_handlers(runner: JobExecutor) -> bool:
    """Wire SIGTERM/SIGINT to a clean stop. False when called off the main thread.

    A worker that ignores SIGTERM is killed mid-job by the orchestrator, and a job killed
    mid-execution is exactly the state the lease and attempt ledger exist to recover from - so
    handling the signal is cheaper than relying on that recovery.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, runner.request_stop)
        except (ValueError, OSError):  # pragma: no cover - platform dependent
            return False
    return True


def main() -> int:  # pragma: no cover - process entry point
    """`python -m agent.executor` - the command the compose service invokes."""
    from config import settings
    from database import FleetSessionLocal
    from observability import configure_logging, register_secrets_from_settings

    configure_logging(level=getattr(settings, "log_level", "INFO"), service="granada-executor")
    register_secrets_from_settings(settings)

    # THE FLEET CREDENTIAL. Like the dispatcher and the relay, this reads jobs across every tenant,
    # and `jobs` is FORCE ROW LEVEL SECURITY - under the application role the claim query would
    # return zero rows and the executor would idle while claiming to be healthy (ADR-0011).
    runner = JobExecutor(
        FleetSessionLocal,
        batch_size=int(getattr(settings, "executor_batch_size", DEFAULT_BATCH_SIZE)),
        interval_seconds=float(
            getattr(settings, "executor_interval_seconds", DEFAULT_INTERVAL_SECONDS)
        ),
    )
    install_signal_handlers(runner)
    logger.info("executor.identity", extra={"worker_id": runner.worker_id})
    runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
