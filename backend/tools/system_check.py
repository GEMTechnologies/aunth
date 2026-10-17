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


def _query(db, sql: str, *, many: bool = False):
    """Run one read-only query, returning ``(value, error)``.

    A FAILED STATEMENT POISONS THE TRANSACTION. PostgreSQL aborts the whole transaction on any error,
    so every subsequent query returns ``InFailedSqlTransaction`` - and the first version of this tool
    reported seven unrelated SKIPs because ONE check had failed. The symptom named the wrong checks:
    a permission problem on the roster made the outbox, the workflows and the catalogue all look
    unmeasurable.

    So every check rolls back on failure and stands on its own. Read-only, so discarding the
    transaction costs nothing.
    """
    from sqlalchemy import text

    try:
        result = db.execute(text(sql))
        return (result.all() if many else result.scalar()), None
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - nothing left to roll back
            pass
        return None, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"[:160]


def _per_tenant(db, sql: str, *, many: bool = False, combine: str = "sum"):
    """Aggregate a query across every organisation, binding one tenant at a time.

    THE TOOL HAD THE BUG IT EXISTS TO CATCH, TWICE. `jobs`, `agent_workflows`, `opportunity_matches`
    and `outbox_events` are all row-level-secured. With no tenant bound, an unprivileged connection
    reads ZERO rows - so the first version reported "no dead-lettered jobs", "no stranded workflows"
    and "matched=0 needs_data=0" while the database held 112 matches; and the outbox check went on
    reporting "no unpublished events" for a table holding 3862 published rows, because
    `max(published_at)` is NULL over an empty visible set.

    That is precisely the failure mode this command was written to expose, so it is fixed the way the
    fleet fixes it: read the roster from the one SECURITY DEFINER function that may answer it, then
    bind each organisation and ask again.

    ``combine`` is "sum" for counts and "min"/"max" for timestamps: adding two timestamps is
    meaningless, and the first version of this helper only knew how to count.
    """
    from sqlalchemy import text

    orgs, error = _query(db, "SELECT DISTINCT org_id FROM fleet_active_agent_ids()", many=True)
    if error:
        return None, error

    if many:
        totals: dict = {}
        for (org_id,) in orgs or []:
            try:
                db.execute(
                    text("SELECT set_config('app.current_org_id', :o, false)"), {"o": org_id}
                )
                for key, count in db.execute(text(sql)).all():
                    totals[key] = totals.get(key, 0) + count
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                return None, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"[:160]
            finally:
                try:
                    db.execute(
                        text("SELECT set_config('app.current_org_id', :o, false)"), {"o": ""}
                    )
                except Exception:  # noqa: BLE001
                    pass
        return list(totals.items()), None

    collected = []
    for (org_id,) in orgs or []:
        try:
            db.execute(text("SELECT set_config('app.current_org_id', :o, false)"), {"o": org_id})
            collected.append(db.execute(text(sql)).scalar())
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            return None, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"[:160]
        finally:
            try:
                db.execute(text("SELECT set_config('app.current_org_id', :o, false)"), {"o": ""})
            except Exception:  # noqa: BLE001
                pass

    present = [value for value in collected if value is not None]
    if not present:
        return None, None
    if combine == "max":
        return max(present), None
    if combine == "min":
        return min(present), None
    return sum(present), None


# ---------------------------------------------------------------------------
# Database checks
# ---------------------------------------------------------------------------
def check_database(report: Report, db) -> None:
    """Every check stands alone.

    A failed statement aborts the PostgreSQL transaction, so one failure used to make every later
    check report ``InFailedSqlTransaction`` - naming seven wrong problems. `_query` rolls back on
    failure, so each check survives its neighbours.
    """
    now = datetime.now(timezone.utc)

    value, error = _query(db, "SELECT version_num FROM alembic_version")
    if error and "permission denied" in error.lower():
        # The runtime role deliberately cannot read alembic_version - that is a security decision,
        # not a fault. Reported as SKIP with the reason, never as a pass.
        report.add("schema at head", SKIP, "the runtime role cannot read alembic_version",
                   "run this check from the migrate container or as the owner")
    elif error:
        report.add("schema at head", FAIL, error, "run: docker compose up migrate")
    else:
        report.add("schema at head", PASS, f"alembic version {value}")

    # A revoked BYPASSRLS with an unbound sweep is the defect that killed the fleet: every query
    # returned zero rows and reported success.
    roster, error = _query(db, "SELECT count(*) FROM fleet_active_agent_ids()")
    if error:
        report.add("fleet can resolve its roster", FAIL, error,
                   "the fleet role cannot read the roster; every sweep would be blind")
    elif not roster:
        report.add("fleet can resolve its roster", FAIL, "0 active agents",
                   "the fleet role cannot see the roster and every sweep is blind")
    else:
        report.add("fleet can resolve its roster", PASS, f"{roster} active agent(s) visible")

    pending, error = _per_tenant(
        db, "SELECT count(*) FROM outbox_events WHERE published_at IS NULL"
    )
    oldest, _ = _per_tenant(
        db, "SELECT min(created_at) FROM outbox_events WHERE published_at IS NULL",
        combine="min",
    )
    # "PENDING = 0" ALONE CANNOT DISTINGUISH A WORKING RELAY FROM A DEAD ONE, and until this used
    # _per_tenant it could not distinguish anything at all: outbox_events is row-level-secured, so an
    # unbound read returned no rows and `max(published_at)` was NULL over 3862 published events.
    # The age of the last successful publish is reported alongside, as information rather than a
    # grade - a genuinely quiet system is not a fault, and warning on it would be crying wolf.
    newest_published, _ = _per_tenant(db, "SELECT max(published_at) FROM outbox_events", combine="max")
    last_published = _age(now, newest_published)
    if error:
        report.add("outbox drained", SKIP, error)
    elif not pending:
        report.add(
            "outbox drained", PASS,
            "no unpublished events"
            + (f"; last publish {last_published} ago" if last_published else ""),
        )
    else:
        age = _age(now, oldest)
        if age and age > timedelta(minutes=15):
            report.add("outbox drained", FAIL, f"{pending} unpublished, oldest {age} old",
                       "the relay is not publishing; check its tenant binding and role")
        else:
            report.add("outbox drained", WARN, f"{pending} unpublished, recent")

    dead, error = _per_tenant(db, "SELECT count(*) FROM jobs WHERE state = 'DEAD_LETTER'")
    if error:
        report.add("no dead-lettered jobs", SKIP, error)
    else:
        report.add(
            "no dead-lettered jobs", PASS if not dead else WARN,
            f"{dead} dead-lettered" if dead else "none",
            "inspect reasons before requeueing; a dead letter is a real failure, not noise",
        )

    # A workflow in RUNNING with no running or queued job behind it is stranded: nothing
    # re-dispatches it, and it looks like work in progress for ever.
    stranded, error = _per_tenant(db, """
        SELECT count(*) FROM agent_workflows w
         WHERE w.state = 'RUNNING'
           AND NOT EXISTS (
               SELECT 1 FROM jobs j
                WHERE j.workflow_id = w.id AND j.state IN ('RUNNING', 'QUEUED')
           )
    """)
    if error:
        report.add("no stranded workflows", SKIP, error)
    else:
        report.add(
            "no stranded workflows", PASS if not stranded else FAIL,
            f"{stranded} RUNNING with no job behind them" if stranded else "none",
            "a dead-lettered job must close its workflow; see AgentWorker.execute",
        )

    active, error = _query(db, "SELECT count(*) FROM opportunities WHERE is_active")
    newest, _ = _query(db, "SELECT max(created_at) FROM opportunities WHERE is_active")
    if error:
        report.add("funding catalogue", SKIP, error)
    else:
        age = _age(now, newest)
        if not active:
            report.add("funding catalogue", FAIL, "no active opportunities",
                       "the discovery sweep is not producing anything")
        elif age and age > timedelta(days=3):
            report.add("funding catalogue", WARN, f"{active} active, newest {age.days}d old",
                       "discovery may be stalled; check the worker's discovery counters")
        else:
            report.add("funding catalogue", PASS,
                       f"{active} active, newest {age} old" if age else f"{active} active")

    rows, error = _per_tenant(
        db, "SELECT state, count(*) FROM opportunity_matches GROUP BY state", many=True
    )
    if error:
        report.add("matching is decidable", SKIP, error)
    else:
        tally = {state: count for state, count in (rows or [])}
        matched = tally.get("MATCHED", 0)
        needs = tally.get("NEEDS_DATA", 0)
        report.add(
            "matching is decidable", PASS if matched else WARN,
            f"matched={matched} needs_data={needs} rejected={tally.get('REJECTED_BY_RULE', 0)}",
            "if everything is NEEDS_DATA, the profile is missing facts the gates read",
        )

    routable, error = _query(db, "SELECT count(*) FROM mail_routing WHERE is_active")
    if error:
        report.add("inbound mailbox routable", SKIP, error)
    else:
        report.add(
            "inbound mailbox routable", PASS if routable else WARN,
            f"{routable} routable address(es)",
            "without one, verification mail is rejected at the door",
        )



def check_model(report: Report) -> None:
    """Are the model routes configured, and is a provider behind them?

    NO API CALL, ON PURPOSE. A status check that spends money every time it runs is a check an
    operator stops running, and the only question it can answer cheaply is "is this configured".
    Reachability was verified separately and by hand.

    Worth checking because a `null` provider is the default: a deployment with no key configured
    does not fail to start, it quietly answers every model request with a refusal - so the agent
    would appear to run and simply never think.
    """
    try:
        from config import settings

        from agent.model_gateway import build_routes_from_settings

        routes = build_routes_from_settings(settings)
    except Exception as exc:  # noqa: BLE001
        report.add("model routes configured", SKIP, f"{type(exc).__name__}: {exc}"[:160])
        return

    if not routes:
        report.add(
            "model routes configured", FAIL, "no tiers configured",
            "set MODEL_PROVIDER and its key, or every model call is refused",
        )
        return

    tiers = ", ".join(sorted(routes))
    provider = (getattr(settings, "model_provider", "") or "").strip()
    if provider.lower() in ("", "null"):
        report.add(
            "model routes configured", FAIL, f"tiers [{tiers}] but provider is {provider or 'unset'}",
            "the agent would run and never think",
        )
    else:
        report.add("model routes configured", PASS, f"{provider} for tiers [{tiers}]")


def check_mailboxes(report: Report, db, now) -> None:
    """Are the organisation's own mailboxes actually being read?

    WHY THIS EXISTS. Connecting an external mailbox is the operator's route around Cloudflare, and every
    way it can fail is SILENT: a wrong app password, a provider that disabled IMAP, a sweep that never
    picks the account up. The account row still says ACTIVE, the mailbox still appears on the Mail
    screen, and mail simply never arrives. That is the exact failure this whole command exists to catch,
    and until now nothing looked.

    The classes are separated rather than collapsed into "unhealthy", because the remedy differs:
    `error` means the provider refused (fix the credential), `never` means the sweep has not tried
    (fix the wiring), `stale` means it worked once and stopped (fix the provider or the credential).
    """
    rows, error = _per_tenant(
        db,
        """
        SELECT CASE
                 WHEN last_sync_at IS NULL AND last_error IS NULL THEN 'never'
                 WHEN last_error IS NOT NULL THEN 'error'
                 WHEN last_sync_at > now() - interval '1 day' THEN 'healthy'
                 ELSE 'stale'
               END AS state,
               count(*)
          FROM mail_accounts
         GROUP BY 1
        """,
        many=True,
    )
    if error:
        report.add("mailboxes readable", SKIP, error)
        return

    counts = {key: value for key, value in (rows or [])}
    total = sum(counts.values())
    if not total:
        report.add(
            "mailboxes readable", PASS,
            "no mailboxes connected",
            "connect one on the Mail screen to read verification mail without Cloudflare",
        )
        return

    problems = []
    if counts.get("error"):
        problems.append(f"{counts['error']} refused by the provider")
    if counts.get("never"):
        problems.append(f"{counts['never']} never synced")
    if counts.get("stale"):
        problems.append(f"{counts['stale']} not synced in a day")

    if not problems:
        report.add("mailboxes readable", PASS, f"{total} connected, all syncing")
        return

    detail = f"{total} connected: " + ", ".join(problems)
    if counts.get("error") or counts.get("never"):
        # A mailbox that was never read, or is being refused, is not "a bit stale" - for the operator
        # relying on it, mail is simply not arriving.
        report.add(
            "mailboxes readable", WARN, detail,
            "open the mailbox's account and re-enter the app password; IMAP must be enabled at the provider",
        )
    else:
        report.add("mailboxes readable", WARN, detail,
                   "the sweep runs every fifteen minutes; check the provider's IMAP access")


# ---------------------------------------------------------------------------
# HTTP checks
# ---------------------------------------------------------------------------
def _get(url: str, timeout: float = 10.0, *, host: str = "") -> tuple[int, str]:
    """GET a URL, optionally with an explicit ``Host`` header.

    THE HOST HEADER IS NOT DECORATION. The API runs TrustedHostMiddleware and answers 400
    "Invalid host header" to any name it does not recognise - so probing it as `http://api:8000`
    from inside the compose network reports the service as broken when it is merely being asked
    politely in the wrong voice. The check has to speak the name the service publishes under.
    """
    request = urllib.request.Request(url)
    if host:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def check_http(report: Report, base: str, *, host: str = "granadac.com") -> None:
    for name, path in (("API health", "/health"), ("API readiness", "/readyz")):
        status, body = _get(base.rstrip("/") + path, host=host)
        if status == 200:
            report.add(name, PASS, body[:120])
        else:
            report.add(name, FAIL, f"HTTP {status}: {body[:120]}",
                       "check the api container and its logs")

    status, body = _get(base.rstrip("/") + "/api/v1/internal/mail/ingest/health", host=host)
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
    parser.add_argument(
        "--host-header",
        default="granadac.com",
        help="Host to present to the API; TrustedHostMiddleware answers 400 to any name it does not know.",
    )
    parser.add_argument("--skip-http", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = Report()

    db = None
    try:
        from database import FleetSessionLocal

        db = FleetSessionLocal()
        check_database(report, db)
        check_mailboxes(report, db, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001
        report.add("database reachable", FAIL, f"{type(exc).__name__}: {exc}"[:200],
                   "check the postgres container and DATABASE_URL")
    finally:
        if db is not None:
            db.close()

    # Configuration, not connectivity: this check never spends a token.
    check_model(report)

    if not args.skip_http:
        check_http(report, args.api_base, host=args.host_header)
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
