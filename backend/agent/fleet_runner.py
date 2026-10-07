"""The fleet's continuous operation: one dispatcher loop, one relay loop.

One loop, not one per customer
------------------------------
The brief is explicit: **do NOT create one timer per GranadaAgent.** There is a
single fleet-level dispatcher process. It wakes on an interval, asks PostgreSQL
what is due across *every* agent, enqueues a bounded batch, and sleeps. Ten
thousand organisations are ten thousand rows in that one query, not ten thousand
scheduled tasks.

Properties this deliberately has:

**Database time, not process time.** Due-ness is evaluated against ``now()``
inside the query. Two dispatchers on machines whose clocks differ by seconds would
otherwise disagree about what is due, and the disagreement would show up as
double dispatch or missed work rather than as an error.

**Bounded batches.** ``batch_size`` and ``per_agent_limit`` cap each sweep, so the
loop cannot load the whole table however many agents exist.

**Interval rather than busy-loop.** The loop sleeps between sweeps. A tight loop
would spend the fleet's whole CPU budget asking a question whose answer changes
every few seconds at most.

**Safe with several processes.** Nothing here holds a lock that another dispatcher
needs. Correctness comes from the durable unique constraints, so running two - or
ten - dispatchers produces the same number of jobs as running one.

**Graceful shutdown.** A stop signal finishes the current sweep and exits, rather
than abandoning a half-written transaction.

**Observable.** It reports its own health and metrics, because "the fleet is
running" is not something an operator should have to infer from silence.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

import models
from agent.workflow_engine import DispatchResult, FleetDispatcher
from observability import metrics

logger = logging.getLogger(__name__)

#: How often to sweep. Short enough that a new opportunity is picked up promptly,
#: long enough that the query is not the busiest thing in the system.
DEFAULT_INTERVAL_SECONDS = 15

#: Cap on a single sleep, so a stop signal is honoured promptly even if the
#: configured interval is long.
MAX_SLEEP_SLICE_SECONDS = 1.0


@dataclass
class FleetHealth:
    """What an operator or a load balancer needs to know."""

    running: bool = False
    sweeps: int = 0
    dispatched: int = 0
    duplicates: int = 0
    skipped_paused: int = 0
    deferred_fairness: int = 0
    errors: int = 0
    last_sweep_at: Optional[datetime] = None
    last_error: Optional[str] = None
    started_at: Optional[datetime] = None
    stopping: bool = False

    @property
    def healthy(self) -> bool:
        """A loop that has never completed a sweep is not healthy, however new."""
        return self.running and not self.stopping and self.sweeps > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "stopping": self.stopping,
            "healthy": self.healthy,
            "sweeps": self.sweeps,
            "dispatched": self.dispatched,
            "duplicates": self.duplicates,
            "skipped_paused": self.skipped_paused,
            "deferred_fairness": self.deferred_fairness,
            "errors": self.errors,
            "last_sweep_at": self.last_sweep_at.isoformat() if self.last_sweep_at else None,
            "last_error": self.last_error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }


class FleetRunner:
    """The fleet-level dispatcher loop.

    Owns a session **per sweep**, not one for the lifetime of the process. A
    long-lived session accumulates an identity map and holds a transaction open
    across sleeps, which on PostgreSQL means an idle-in-transaction connection and
    on any database means stale reads.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        batch_size: int = 200,
        per_agent_limit: int = 25,
        on_sweep: Optional[Callable[[DispatchResult], None]] = None,
    ) -> None:
        self.session_factory = session_factory
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.batch_size = batch_size
        self.per_agent_limit = per_agent_limit
        self.on_sweep = on_sweep
        self.health = FleetHealth()
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def sweep_once(self) -> DispatchResult:
        """One sweep in its own session and transaction.

        The session factory call is **inside** the guard. It was outside, which
        meant a database that was unreachable at the moment of checkout raised past
        the error counter entirely: the loop survived (an outer handler swallowed
        it) but ``errors`` stayed zero, so an operator saw a dispatcher that looked
        healthy and had silently dispatched nothing all night.
        """
        db: Optional[Session] = None
        try:
            db = self.session_factory()
            dispatcher = FleetDispatcher(
                db, batch_size=self.batch_size, per_agent_limit=self.per_agent_limit
            )
            result = dispatcher.dispatch_once()
            db.commit()
            self.health.sweeps += 1
            self.health.dispatched += result.dispatched
            self.health.duplicates += result.duplicates
            self.health.skipped_paused += result.skipped_paused
            self.health.last_sweep_at = datetime.now(timezone.utc)
            if self.on_sweep is not None:
                self.on_sweep(result)
            return result
        except Exception as exc:
            if db is not None:
                try:
                    db.rollback()
                except Exception:  # pragma: no cover - the connection is already gone
                    pass
            self.health.errors += 1
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            # A failed sweep must not kill the loop: the next one may succeed, and
            # a dispatcher that exits on a transient database blip stops the whole
            # fleet for every customer.
            logger.warning(
                "fleet.sweep_failed",
                extra={"error": self.health.last_error, "sweeps": self.health.sweeps},
            )
            raise
        finally:
            if db is not None:
                db.close()

    # ------------------------------------------------------------------
    def run_forever(self, *, max_sweeps: Optional[int] = None) -> FleetHealth:
        """Sweep, sleep, repeat - until asked to stop.

        ``max_sweeps`` exists for tests and one-shot drains; production passes
        nothing and relies on the stop signal.
        """
        self.health.running = True
        self.health.started_at = datetime.now(timezone.utc)
        self._stop.clear()
        logger.info(
            "fleet.started",
            extra={"interval_seconds": self.interval_seconds, "batch_size": self.batch_size},
        )
        try:
            while not self._stop.is_set():
                try:
                    self.sweep_once()
                except Exception:
                    pass  # already counted and logged; the loop continues
                if max_sweeps is not None and self.health.sweeps >= max_sweeps:
                    break
                self._sleep_until_next_sweep()
        finally:
            self.health.running = False
            logger.info("fleet.stopped", extra=self.health.as_dict())
        return self.health

    def _sleep_until_next_sweep(self) -> None:
        """Sleep in slices so a stop signal is honoured promptly.

        ``Event.wait`` returns as soon as the event is set, so this costs nothing
        and exits within a slice rather than after a full interval.
        """
        remaining = self.interval_seconds
        while remaining > 0 and not self._stop.is_set():
            slice_seconds = min(MAX_SLEEP_SLICE_SECONDS, remaining)
            if self._stop.wait(slice_seconds):
                return
            remaining -= slice_seconds

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Ask the loop to finish its current sweep and exit."""
        self.health.stopping = True
        self._stop.set()

    def request_stop(self, *_args: Any) -> None:
        """Signal-handler shaped, so it can be wired to SIGTERM directly."""
        self.stop()


def install_signal_handlers(runner: FleetRunner) -> bool:
    """Wire SIGTERM/SIGINT to a graceful stop.

    Returns whether it succeeded. Signal handlers can only be installed from the
    main thread of the main interpreter, so a runner started from a worker thread
    (which is how tests exercise it) must not fail - it simply is not signal-driven.
    """
    installed = False
    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, runner.request_stop)
            installed = True
        except (ValueError, OSError):  # not the main thread
            return installed
    return installed


def main() -> int:  # pragma: no cover - process entry point
    """Run the dispatcher loop as a process.

    Kept deliberately thin: everything testable lives in ``FleetRunner``, and this
    only wires configuration, logging and signals.
    """
    import logging as _logging

    from config import settings
    from database import SessionLocal
    from observability import configure_logging, register_secrets_from_settings

    configure_logging(level=getattr(settings, "log_level", "INFO"), service="granada-fleet")
    register_secrets_from_settings(settings)

    runner = FleetRunner(
        SessionLocal,
        interval_seconds=float(getattr(settings, "fleet_dispatch_interval_seconds", DEFAULT_INTERVAL_SECONDS)),
        batch_size=int(getattr(settings, "fleet_dispatch_batch_size", 200)),
        per_agent_limit=int(getattr(settings, "fleet_per_agent_limit", 25)),
    )
    install_signal_handlers(runner)
    runner.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
