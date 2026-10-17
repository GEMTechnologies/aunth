"""Sync one mailbox now, through the real dispatch and execute path.

WHY THIS EXISTS. Verifying a connected mailbox by hand meant editing `agent_workflows` directly -
setting it PENDING and moving `next_run_at` into the present. That collided with job idempotency (the
job key includes the attempt), so the dispatcher marked the workflow RUNNING while *no new job was
created*, and the workflow then sat there looking busy and doing nothing. Twice. A test that mutates
the state machine it is testing is not a test.

This drives the same path the fleet drives - schedule, dispatch, execute - and reports what actually
happened. It is also the operator tool for "I connected a mailbox, fetch it now": the sweep is on a
fifteen-minute cycle, and waiting is how a real problem gets mistaken for slowness.

    docker compose exec -T worker python /app/backend/tools/mail_sync_once.py --org <uuid>
    docker compose exec -T worker python /app/backend/tools/mail_sync_once.py --org <uuid> --address ngo@example.org

Exits non-zero when the mailbox could not be synced, because a command whose whole purpose is to find
out whether mail is readable must not report success when it is not.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def sync_once(org_id: str, address: str | None, *, dispatch: bool = True) -> dict:
    from database import FleetSessionLocal
    from tenant_context import set_tenant

    import models
    from agent.mail.gateway import schedule_mail_sync
    from agent.workflow_engine import AgentWorker, FleetDispatcher

    db = FleetSessionLocal()
    try:
        # THE BINDING IS NOT OPTIONAL: mail_accounts, agent_workflows and jobs are all row-level
        # secured, and `app.mail_credential_ciphertext()` answers only for the bound organisation.
        set_tenant(db, org_id, None)

        query = db.query(models.MailAccount).filter(models.MailAccount.org_id == org_id)
        if address:
            query = query.filter(models.MailAccount.address == address.lower())
        account = query.first()
        if account is None:
            return {"ok": False, "error": f"no mailbox matched in {org_id}"}

        before = {
            "address": account.address,
            "sync_status": account.sync_status,
            "last_sync_at": account.last_sync_at.isoformat() if account.last_sync_at else None,
            "last_error": account.last_error,
        }

        # 1. Ask the real scheduler for work. It is the production path and it is the thing that was
        #    silently skipping connected mailboxes.
        queued = schedule_mail_sync(db, stale_seconds=60, batch_size=5, org_id=org_id)
        db.commit()

        workflow = (
            db.query(models.AgentWorkflow)
            .filter(
                models.AgentWorkflow.org_id == org_id,
                models.AgentWorkflow.workflow_type == "mail_sync",
                models.AgentWorkflow.subject_id == account.id,
            )
            .order_by(models.AgentWorkflow.created_at.desc())
            .first()
        )
        if workflow is None:
            return {
                "ok": False,
                "error": "the scheduler queued nothing for this mailbox",
                "queued": queued,
                "before": before,
                "hint": "a mailbox with no credential_ref and no registered transport is skipped by design",
            }

        # 2. Make it due and let the REAL dispatcher create the job - no hand-edited state machine.
        workflow.next_run_at = datetime.now(timezone.utc)
        db.commit()

        job_id = None
        if dispatch:
            FleetDispatcher(db, use_tenant_binding=True).dispatch_once()
            db.commit()
            job = (
                db.query(models.Job)
                .filter(models.Job.workflow_id == workflow.id)
                .order_by(models.Job.created_at.desc())
                .first()
            )
            job_id = job.id if job else None

        # 3. Execute it the way the executor does.
        executed = None
        if job_id:
            result = AgentWorker(db, worker_id="mail-sync-once").execute(job_id)
            executed = {
                "ok": getattr(result, "ok", None),
                "state": getattr(result, "state", None),
                "summary": (getattr(result, "summary", "") or "")[:200],
            }
            db.commit()

        db.refresh(account)
        db.refresh(workflow)
        after = {
            "sync_status": account.sync_status,
            "last_sync_at": account.last_sync_at.isoformat() if account.last_sync_at else None,
            "last_error": account.last_error,
            "workflow_state": workflow.state,
            "waiting_on": workflow.waiting_on,
        }

        # The mailbox was read if the account recorded a sync, or if the workflow moved on with
        # something other than "no transport" - which is the defect this command was built to expose.
        progressed = bool(after["last_sync_at"]) or bool(after["last_error"]) or bool(
            executed and executed.get("ok")
        )
        return {
            "ok": progressed,
            "address": account.address,
            "queued": queued,
            "job": job_id,
            "executed": executed,
            "before": before,
            "after": after,
        }
    finally:
        db.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Sync one mailbox through the real dispatch path.")
    parser.add_argument("--org", required=True, help="organisation id")
    parser.add_argument("--address", default=None, help="mailbox address (default: the first one)")
    parser.add_argument("--no-dispatch", action="store_true", help="schedule only")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    outcome = sync_once(args.org, args.address, dispatch=not args.no_dispatch)
    if args.json:
        print(json.dumps(outcome, indent=2))
    else:
        if not outcome.get("ok"):
            print(f"FAIL  {outcome.get('error') or outcome.get('after', {}).get('waiting_on') or 'not synced'}")
        else:
            print(f"OK    {outcome['address']}")
        for section in ("before", "after"):
            if section in outcome:
                print(f"  {section}: {json.dumps(outcome[section])}")
        if outcome.get("executed"):
            print(f"  executed: {json.dumps(outcome['executed'])}")
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
