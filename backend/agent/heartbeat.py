"""A liveness signal for the background workers, so Docker can tell a wedged loop from a busy one.

THE DEFECT THIS FIXES
---------------------
The image carries one HEALTHCHECK, inherited by every service:

    CMD curl -fsS "http://127.0.0.1:${PORT}/livez"

That is right for the API and **wrong for the worker and the relay**, which are background loops and
listen on no port at all. Docker therefore reported both `unhealthy` while they were working
perfectly - and in an orchestrator that is a restart loop, killing a healthy process every 90 seconds.

WHAT A WORKER'S LIVENESS ACTUALLY IS
------------------------------------
Not "is a port open". It is **"is the loop still going round"**. Those are different questions, and
the difference matters: a worker can hold its process open while the loop is stuck - a wedged
database call, an unhandled exception swallowed by the loop, a sleep that never returns. A port check
would call that healthy; a heartbeat calls it what it is.

Both runners already track this internally (`FleetHealth`, `RelayHealth`, incremented per sweep). This
makes it visible to the outside, by touching a file after every completed sweep. `docker compose`
then checks the file is fresh.

The staleness threshold is deliberately generous - several sweeps' worth - because a slow sweep is not
a dead worker, and a healthcheck that cries wolf is worse than none.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

#: Where the heartbeat is written. `/tmp` because it is per-container and needs no volume: the signal
#: is about THIS process, and it should not survive a restart - a stale file from a previous run would
#: report a dead worker as healthy.
DEFAULT_HEARTBEAT_PATH = Path("/tmp/granada-heartbeat")


def heartbeat_path() -> Path:
    """The configured path, so a test can point it somewhere harmless."""
    override = os.environ.get("GRANADA_HEARTBEAT_PATH")
    return Path(override) if override else DEFAULT_HEARTBEAT_PATH


def beat(path: Path | None = None, *, now: float | None = None) -> None:
    """Record that a sweep completed. Never raises.

    A worker must not die because it could not write a status file: the failure that matters is the
    work, and a liveness signal that can kill the thing it observes has the relationship backwards.
    The consequence of a failed write is that the container reports unhealthy - which is a false
    alarm, and better than a dead worker.
    """
    target = path or heartbeat_path()
    try:
        target.write_text(str(now if now is not None else time.time()), encoding="utf-8")
    except OSError:
        pass


def is_fresh(*, path: Path | None = None, max_age_seconds: float, now: float | None = None) -> bool:
    """True when a sweep has completed recently enough to call the loop alive."""
    target = path or heartbeat_path()
    try:
        recorded = float(target.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    current = now if now is not None else time.time()
    return (current - recorded) <= max_age_seconds
