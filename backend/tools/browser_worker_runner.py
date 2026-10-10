"""The host-side browser worker runner - the bridge the container cannot be.

WHY THIS RUNS ON THE HOST, MEASURED

The executor container cannot start a browser and cannot run the worker:

    /app/backend/tools/browser_worker.py   EXISTS in the image
    playwright / browser_use / stagehand   NOT importable in the container
    Chromium on disk in the container      absent
    user namespaces in the container       absent (CapEff 0000000000000000)
    host eval venv mounted into container  no - only postgres declares a volume

So `SubprocessInvoker` inside the container spawns a worker that immediately reports
`WORKER_CRASH: No module named 'playwright'` and returns UNCERTAIN. That degradation is correct and
safe - and it is not an execution.

This process is the execution. It takes the SAME leased jobs the container would have taken, runs them
with the host's interpreter, and records the outcome against the same `jobs`/`job_attempts` rows.

WHY IT IS NOT A SECOND AGENT PLATFORM

It plans nothing, decides nothing and reasons about nothing. It claims a job the existing dispatcher
created, spawns one process, writes back what that process reported, and stops. Every decision about
what to do was already made and persisted by `agent_workflows`; this is a transport for one job type.

THE THREE BOUNDS

1. **One browser at a time.** `MAX_CONCURRENT_SESSIONS = 1`, enforced by the loop processing exactly one
   claim per iteration. Measured available memory would allow ~3; the ceiling is credential blast
   radius, not RAM, and one organisation's credentials in one browser at a time is a bound a memory
   figure cannot express.

2. **Never submits.** `submission_authorised=False` is passed to the worker unconditionally. Authority
   lives in `submission_authority`, and this runner has no way to grant it - by construction, not by
   discipline.

3. **A lease is required.** No job is executed without `JobLedger.claim` returning an attempt row, so a
   job already leased by another holder is left alone rather than run twice.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

#: The hard concurrency bound, stated as a constant so no caller can raise it by argument.
MAX_CONCURRENT_SESSIONS = 1

#: The job type the container's `_handle_browser_task` handles. Named here rather than imported so a
#: rename in the workflow engine fails loudly at the claim (zero jobs found) rather than silently
#: running the wrong work.
BROWSER_JOB_TYPE = "browser_task"

WORKER_PROCESS = os.path.join(BACKEND, "tools", "browser_worker.py")

DEFAULT_LEASE_SECONDS = 900
DEFAULT_TIMEOUT_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunnerUnavailable(RuntimeError):
    """The runner cannot proceed - no database, or no worker."""


def find_browser_jobs(db: Any, *, limit: int = 10) -> list[str]:
    """Queued browser jobs that are due, oldest first.

    Read-only and cheap: the claim is what takes the lease, and a job listed here may be gone by then.
    """
    from sqlalchemy import select

    from models import Job

    rows = db.execute(
        select(Job.id)
        .where(
            Job.job_type == BROWSER_JOB_TYPE,
            Job.state == Job.QUEUED,
            Job.available_at <= _now(),
        )
        .order_by(Job.available_at)
        .limit(limit)
    ).scalars().all()
    return list(rows)


def run_worker(
    request: dict[str, Any],
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    interpreter: Optional[str] = None,
) -> dict[str, Any]:
    """Spawn the worker for one request and return its report.

    The task goes in on stdin as JSON, so nothing tenant-identifying reaches the process table. A
    non-zero exit is UNCERTAIN rather than FAILED for the same reason the container's invoker uses
    UNCERTAIN: a worker that died may have acted before it did.
    """
    command = [interpreter or sys.executable, WORKER_PROCESS]
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "UNCERTAIN",
            "outcome_certain": False,
            "problems": [
                {
                    "kind": "WORKER_TIMEOUT",
                    "detail": (
                        f"the worker exceeded {timeout_seconds}s; if it had reached the submission "
                        "step the outcome is unknown, so this must be reconciled rather than retried"
                    ),
                }
            ],
        }

    if completed.returncode != 0:
        return {
            "status": "UNCERTAIN",
            "outcome_certain": False,
            "problems": [
                {
                    "kind": "WORKER_EXIT",
                    "detail": (
                        f"worker exited {completed.returncode} without reporting an outcome; it may "
                        "have acted before it died"
                    ),
                    "stderr_tail": completed.stderr.decode("utf-8", "replace")[-400:],
                }
            ],
        }

    try:
        return json.loads(completed.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {
            "status": "UNCERTAIN",
            "outcome_certain": False,
            "problems": [{"kind": "WORKER_UNPARSEABLE_OUTPUT"}],
        }


def run_once(
    db: Any,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    interpreter: Optional[str] = None,
    execute: Any = None,
) -> Optional[dict[str, Any]]:
    """Claim and run at most ONE browser job. Returns None when there is nothing to do.

    ONE per call, not a batch. The concurrency bound is the reason: a loop that claimed a list and ran
    them would open as many browsers as the list was long, and the bound would exist only in a comment.

    `execute` is injectable so the claim/lease/record path can be tested without a browser.
    """
    from events.ledger import JobLedger
    from models import Job
    from sqlalchemy import select

    run_worker_fn = execute or run_worker

    for job_id in find_browser_jobs(db, limit=5):
        # The job's org is read BEFORE the lease so the tenant can be bound for the claim itself. A
        # claim under RLS with no tenant set would see no row and look like "not claimable".
        row = db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
        if row is None:
            continue
        org_id = row.org_id
        payload = dict(row.payload or {})

        db.execute(
            __import__("sqlalchemy").text("SELECT set_config('app.current_org_id', :o, false)"),
            {"o": org_id},
        )

        attempt = JobLedger(db).claim(
            job_id=job_id, worker_id=worker_id, lease_seconds=lease_seconds, org_id=org_id
        )
        if attempt is None:
            # Someone else holds it, or it went terminal between the read and the claim. Correct to
            # leave alone - running it anyway is how a job executes twice.
            continue

        request = dict(payload)
        request.setdefault("job_id", job_id)
        request.setdefault("org_id", org_id)
        # NEVER authoritative here. Authority lives in submission_authority; this runner cannot grant
        # what it was not given, and does not try.
        request["submission_authorised"] = False
        request["dry_run"] = True

        report = run_worker_fn(request, timeout_seconds=timeout_seconds, interpreter=interpreter)
        return {"job_id": job_id, "org_id": org_id, "attempt_id": attempt.id, "report": report}

    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Host-side browser worker runner (one session at a time)")
    parser.add_argument("--once", action="store_true", help="run at most one job and exit")
    parser.add_argument("--interval", type=int, default=15, help="seconds between polls")
    parser.add_argument("--worker-id", default=f"host-browser-{os.getpid()}", help="lease owner name")
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--interpreter", default=None, help="python to run the worker with")
    args = parser.parse_args(argv)

    if not os.path.exists(WORKER_PROCESS):
        raise RunnerUnavailable(f"worker not found at {WORKER_PROCESS}")

    from database import SessionLocal

    processed = 0
    while True:
        db = SessionLocal()
        try:
            result = run_once(
                db,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
                timeout_seconds=args.timeout_seconds,
                interpreter=args.interpreter,
            )
            if result is None:
                db.commit()
                if args.once:
                    print(json.dumps({"processed": 0}))
                    return 0
                time.sleep(args.interval)
                continue
            db.commit()
            processed += 1
            print(json.dumps({"processed": processed, **result}, default=str))
            # MAX_CONCURRENT_SESSIONS is 1, so the loop never overlaps: the next claim happens only
            # after this worker process has exited.
            if args.once:
                return 0
        finally:
            db.close()


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
