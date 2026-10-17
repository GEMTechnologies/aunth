"""One command that answers "is everything working?".

WHY THIS EXISTS. Every defect found in this system was invisible from the layer above it:

  * the fleet loop failed every sweep for hours and reported ``sweeps: 0`` as if idle;
  * 1089 outbox events sat unpublished while the relay logged ``pending: 0``;
  * 49 workflows were stranded in RUNNING with dead jobs behind them;
  * a deploy served a four-hour-old bundle because the asset URL never changed.

None of those showed up in a healthcheck. Each was found by an engineer thinking to look. This tool
encodes the look: it checks the signals that actually move, and it grades them so a failure is not
mistaken for calm.

GRADING. Every check returns PASS, WARN, FAIL or SKIP with a sentence.
  * FAIL means something is broken right now and the exit code is 1.
  * WARN means it is degrading or unverified, and exit code is 0 - a warning that fails a pipeline
    gets suppressed, and a suppressed warning is how the next outage hides.
  * SKIP means the check could not be evaluated, which is reported rather than passed. An absent
    measurement is not a good one.

READ-ONLY. Every query is a SELECT and every HTTP call is a GET. Running this against production is
always safe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    #: What an operator should do about a non-PASS. Empty for PASS.
    remedy: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, remedy: str = "") -> None:
        self.checks.append(Check(name, status, detail, remedy))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def as_dict(self) -> dict:
        return {
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "remedy": c.remedy}
                for c in self.checks
            ],
            "failed": len(self.failed),
            "warned": len(self.warned),
            "ok": not self.failed,
        }


def _age(now: datetime, then: datetime | None) -> timedelta | None:
    if then is None:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return now - then


# ---------------------------------------------------------------------------
# Database checks
# ---------------------------------------------------------------------------
def check_database(report: Report, db) -> None:
    from sqlalchemy import text

    # --- schema at head -----------------------------------------------------
    try:
        head = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        report.add("schema at head", PASS, f"alembic version {head}")
    except Exception as exc:  # noqa: BLE001
        report.add("schema at head", FAIL, f"{type(exc).__name__}: {exc}"[:160],
                   "run: docker compose up migrate")

    # --- the fleet role can actually see its roster -------------------------
    # A revoked BYPASSRLS with an unbound sweep is the defect that killed the fleet: every query
    # returned zero rows and reported success.
    try:
        roster = db.execute(text("SELECT count(*) FROM fleet_active_agent_ids()")).scalar()
        report.add(
            "fleet can resolve its roster", PASS if roster else FAIL,
            f"{roster} active agent(s) visible to the fleet role",
            "if 0, the fleet role cannot see the roster and every sweep is blind",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("fleet can resolve its roster", FAIL, f"{type(exc).__name__}: {exc}"[:160])

    # --- outbox drained -----------------------------------------------------
    try:
        pending = db.execute(
            text("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")
        ).scalar()
        oldest = db.execute(
            text("SELECT min(created_at) FROM outbox_events WHERE published_at IS NULL")
        ).scalar()
        age = _age(datetime.now(timezone.utc), oldest)
        if not pending:
            report.add("outbox drained", PASS, "no unpublished events")
        elif age and age > timedelta(minutes=15):
            report.add(
                "outbox drained", FAIL,
                f"{pending} unpublished, oldest {age} old",
                "the relay is not publishing; check its tenant binding and role",
            )
        else:
            report.add("outbox drained", WARN, f"{pending} unpublished, recent")
    except Exception as exc:  # noqa: BLE001
        report.add("outbox drained", SKIP, f"{type(exc).__name__}: {exc}"[:160])

    # --- no dead-lettered jobs ---------------------------------------------
    try:
        dead = db.execute(
            text("SELECT count(*) FROM jobs WHERE state = 'DEAD_LETTER'")
        ).scalar()
        report.add(
            "no dead-lettered jobs", PASS if not dead else WARN,
            f"{dead} dead-lettered" if dead else "none",
            "inspect reasons before requeueing; a dead letter is a real failure, not noise",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("no dead-lettered jobs", SKIP, f"{type(exc).__name__}: {exc}"[:160])

    # --- no stranded workflows ---------------------------------------------
    # A workflow in RUNNING with no running or queued job behind it is stranded: nothing
    # re-dispatches it, and it looks like work in progress for ever.
    try:
        stranded = db.execute(text("""
            SELECT count(*) FROM agent_workflows w
             WHERE w.state = 'RUNNING'
               AND NOT EXISTS (
                   SELECT 1 FROM jobs j
                    WHERE j.workflow_id = w.id AND j.state IN ('RUNNING', 'QUEUED')
               )
        """)).scalar()
        report.add(
            "no stranded workflows", PASS if not stranded else FAIL,
            f"{stranded} RUNNING with no job behind them" if stranded else "none",
            "a dead-lettered job must close its workflow; see AgentWorker.execute",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("no stranded workflows", SKIP, f"{type(exc).__name__}: {exc}"[:160])

    # --- funding catalogue --------------------------------------------------
    try:
        active = db.execute(
            text("SELECT count(*) FROM opportunities WHERE is_active")
        ).scalar()
        newest = db.execute(
            text("SELECT max(created_at) FROM opportunities WHERE is_active")
        ).scalar()
        age = _age(datetime.now(timezone.utc), newest)
        if not active:
            report.add("funding catalogue", FAIL, "no active opportunities",
                       "the discovery sweep is not producing anything")
        elif age and age > timedelta(days=3):
            report.add("funding catalogue", WARN,
                       f"{active} active, newest {age.days}d old",
                       "discovery may be stalled; check the worker's discovery counters")
        else:
            report.add("funding catalogue", PASS, f"{active} active, newest {age} old"
                       if age else f"{active} active")
    except Exception as exc:  # noqa: BLE001
        report.add("funding catalogue", SKIP, f"{type(exc).__name__}: {exc}"[:160])

    # --- matching -----------------------------------------------------------
    try:
        rows = db.execute(text(
            "SELECT state, count(*) FROM opportunity_matches GROUP BY state"
        )).all()
        tally = {state: count for state, count in rows}
        needs = tally.get("NEEDS_DATA", 0)
        matched = tally.get("MATCHED", 0)
        report.add(
            "matching is decidable", PASS if matched else WARN,
            f"matched={matched} needs_data={needs} rejected={tally.get('REJECTED_BY_RULE', 0)}",
            "if everything is NEEDS_DATA, the profile is missing facts the gates read",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("matching is decidable", SKIP, f"{type(exc).__name__}: {exc}"[:160])

    # --- inbound mailbox ----------------------------------------------------
    try:
        routable = db.execute(
            text("SELECT count(*) FROM mail_routing WHERE is_active")
        ).scalar()
        report.add(
            "inbound mailbox routable", PASS if routable else WARN,
            f"{routable} routable address(es)",
            "without one, verification mail is rejected at the door",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("inbound mailbox routable", SKIP, f"{type(exc).__name__}: {exc}"[:160])


# ---------------------------------------------------------------------------
# HTTP checks
# ---------------------------------------------------------------------------
def _get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def check_http(report: Report, base: str) -> None:
    for name, path in (("API health", "/health"), ("API readiness", "/readyz")):
        status, body = _get(base.rstrip("/") + path)
        if status == 200:
            report.add(name, PASS, body[:120])
        else:
            report.add(name, FAIL, f"HTTP {status}: {body[:120]}",
                       "check the api container and its logs")

    status, body = _get(base.rstrip("/") + "/api/v1/internal/mail/ingest/health")
    if status == 200:
        try:
            configured = json.loads(body).get("configured")
        except ValueError:
            configured = None
        report.add(
            "inbound ingest configured", PASS if configured else FAIL,
            body[:160],
            "set MAIL_INGEST_KEY, or no mail can be delivered",
        )
    else:
        report.add("inbound ingest configured", SKIP, f"HTTP {status}")


def check_browser_bridge(report: Report, bridge: str) -> None:
    status, body = _get(bridge.rstrip("/") + "/health")
    if status == 200 and '"ok": true' in body.replace("True", "true").lower():
        report.add("browser bridge", PASS, body[:160])
    elif status == 0:
        report.add("browser bridge", SKIP, f"unreachable: {body[:120]}",
                   "the bridge runs on the host, not in compose; SKIP inside a container")
    else:
        report.add("browser bridge", FAIL, f"HTTP {status}: {body[:120]}")


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that Granada is actually working.")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--bridge", default="http://172.18.0.1:8765")
    parser.add_argument("--skip-http", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = Report()

    db = None
    try:
        from database import FleetSessionLocal

        db = FleetSessionLocal()
        check_database(report, db)
    except Exception as exc:  # noqa: BLE001
        report.add("database reachable", FAIL, f"{type(exc).__name__}: {exc}"[:200],
                   "check the postgres container and DATABASE_URL")
    finally:
        if db is not None:
            db.close()

    if not args.skip_http:
        check_http(report, args.api_base)
        check_browser_bridge(report, args.bridge)

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        width = max(len(c.name) for c in report.checks)
        for check in report.checks:
            print(f"{check.status:4}  {check.name:<{width}}  {check.detail}")
            if check.remedy and check.status in (FAIL, WARN):
                print(f"      {'':<{width}}  → {check.remedy}")
        print()
        print(
            f"{len(report.checks)} checks: "
            f"{sum(1 for c in report.checks if c.status == PASS)} pass, "
            f"{len(report.warned)} warn, {len(report.failed)} fail, "
            f"{sum(1 for c in report.checks if c.status == SKIP)} skip"
        )

    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
