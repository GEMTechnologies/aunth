# ADR-0011 narrowing: rollout and rollback

**Status:** plan approved; **narrow function applied additively; equivalence now PROVEN over a
non-empty set; cutover and `BYPASSRLS` revoke deliberately NOT done**
**Date:** 2026-10-09 (equivalence evidence added same day)
**Scope:** the `granada_fleet` database role's `BYPASSRLS`, and the browser worker's credentials

## Where this now stands (added 2026-10-09)

Three states matter, and they are different:

| Step | State |
|---|---|
| Narrow function `fleet_due_job_ids(batch_size)` applied | ✅ additive, production |
| Dispatcher cut over to call it | ⬜ not done |
| `BYPASSRLS` revoked from `granada_fleet` | ⬜ not done |
| **Browser worker forbidden the wide role** | ✅ **enforced in code and verified live** |

### The equivalence check is now conclusive

It was reported `IDENTICAL` twice before **over an empty set** — 0 due, next due `none`. Two empty sets
are always equal, so those results proved nothing. That was flagged rather than counted as done, and
this round made it real: a due job **and** a future job inserted inside a transaction, compared, then
**rolled back** so production was untouched.

```
PROBE: function = 1
PROBE: direct   = 1
PROBE EQUIVALENCE = IDENTICAL
PROBE: both contain it     = YES    <- set comparison, not two zeros
PROBE: future job excluded = YES    <- the function does not hand out work early
AFTER ROLLBACK: jobs = 1273, due = 0, probe rows = 0
```

The `future job excluded` case is a correctness property, not a privilege one: a narrow function that
released work early would break the dispatcher's contract in a way a privilege review would not catch.

### Why the cutover is not done

It touches the dispatcher's claim path in production. Doing it correctly needs the code change, the
full regression, the rollback rehearsal and a deployment — and the evidence for it had to be honest
before any of that started. The evidence is now in place; the change is unstarted rather than
half-finished, which is the state a security migration should be left in when it is not completed.

### The restriction already enforced before any worker gets credentials

§12 asks for the exact restrictions required before a live browser worker receives tenant
credentials. One of them is no longer documentation — it is a startup refusal
(`agent/worker_privileges.py`):

    granada_fleet  bypassrls = TRUE    the widest role in the system
    granada_app    bypassrls = FALSE   already exists

The browser opens a live portal, reads an organisation's documents, holds their credentials and types
their data into a form. It needs **no** cross-tenant visibility: everything arrives in its task
payload. A worker connecting as `granada_fleet` would be a privilege escalation by **configuration**,
and it now refuses to open a page:

```
$ FLEET_DATABASE_URL=postgresql://granada_fleet:pw@db/granada_auth browser_worker.py < {}
{"status": "REJECTED", ..., "problems": [{"kind": "PRIVILEGE_REFUSED", ...}]}
```

Its limit is recorded rather than implied away: it cannot detect a role granted **after** startup.

## The problem, measured

Verified live on the production VPS on 2026-10-09:

```
ROLE granada_app    bypassrls=false  super=false  canlogin=true
ROLE granada_fleet  bypassrls=true   super=false  canlogin=true
ROLE granada_user   bypassrls=false  super=false  canlogin=true

RLS enabled on 26+ tables:
  documents · jobs · job_attempts · agent_workflows · applications
  application_transitions · audit_logs · mail_approvals · mail_messages
  granada_agents · grants · disbursements · agent_activity · decision_records …
```

`granada_fleet` is not superuser, so table grants still constrain it — but `BYPASSRLS` defeats the row
policies on every table that holds tenant data. A connection as that role sees every organisation's
documents, applications and approvals unfiltered.

`docker-compose.yml` states why it was done:

> `# Same credential as the worker and the relay, and for the same reason: jobs is FORCE ROW LEVEL`
> `# SECURITY, so under the application role this executor's claim query would return zero`

The comment is correct about the constraint. The concern is that the *execution* phase inherits the
same credential as the *discovery* phase, when only discovery needs it.

## What actually needs cross-tenant visibility

Inspected in code, not assumed (`agent/fleet_runner.py`, `agent/executor.py`):

```
fleet_runner.py   "what is due across *every* agent, enqueues a bounded batch"
fleet_runner.py   "DISCOVER, THEN CLAIM."
executor.py       "Claiming is the database's job, not this module's."
executor.py       "JobLedger.claim, which takes a lease with a unique constraint between …"
executor.py       "the same job is therefore resolved by PostgreSQL, and the loser gets …"
```

**One operation requires cross-tenant visibility: discovery** — asking *which* work is due, across
organisations. Everything after that is single-tenant:

| Phase | Needs cross-tenant read? | Why |
|---|---|---|
| Discovery / sweep | **yes** | must find due work for *every* organisation |
| Claim | **no** | resolved by lease + unique constraint, not by reading tenant rows |
| Execution | **no** | the job belongs to exactly one organisation |
| Browser execution | **no** | touches documents, credentials, packages — the data that must stay isolated |

`validate_task`'s own docstring in `agent/browser_boundary.py` already encodes this principle:

> *"This function cannot look them up itself — doing so would require the cross-tenant privilege the
> directive forbids the worker from having."*

The boundary already refuses to self-authorise. This plan makes the database agree with it.

## The narrow architecture

1. **A `SECURITY DEFINER` function returning IDENTIFIERS ONLY.**

   ```sql
   -- Returns job ids due for work. No organisation names, no document ids, no payloads.
   CREATE FUNCTION fleet_due_job_ids(batch_size int) RETURNS TABLE (job_id uuid)
   ```

   Owned by a role that may see across tenants. `SECURITY DEFINER` means the *function* runs with
   that privilege, so the caller does not need it — which is the whole point. `SET search_path` is
   pinned so the definer's privileges cannot be redirected via a hostile schema.

2. **The dispatcher calls only that function.** It never selects from `jobs` directly across tenants,
   so its own connection can be `granada_app`.

3. **The worker binds the tenant per job** and runs with RLS fully enforced, using the existing
   `require_org_access` idiom.

4. **Contention stays in the unique constraint and lease** — already correct, needs no privilege.

5. **The browser worker connects as `granada_app`** (`BYPASSRLS=false`, already exists). It touches
   documents and credentials; it must never hold the wide role. This is a hard requirement, not a
   preference, and it costs nothing because the role is already right.

## Compatibility and risk

| Risk | Assessment |
|---|---|
| Breaking the claim path | The claim query runs under the worker's own role after binding; the function is additive |
| Function performance | Returns ids from an indexed due-work query; bounded by `batch_size` |
| `SECURITY DEFINER` misuse | `search_path` pinned; body returns only ids; `EXECUTE` granted narrowly |
| Locking / downtime | `CREATE FUNCTION` takes no table lock and does not rewrite data — no downtime expected |
| Rollback | `DROP FUNCTION` restores the previous state exactly; no data migration is involved |

**No table is altered and no row is migrated, so rollback is a single `DROP FUNCTION`.** That is the
property that makes this safe to attempt, and it is why this change was chosen over any redesign that
touches table ownership or policy definitions.

## Rollout sequence

Each step is separately verifiable, and any step can be the last one.

1. **Apply the function, grant nothing.** Verify it exists and returns ids.
2. **Verify it returns the SAME set as the current dispatcher query** for a fixed input — a direct
   equivalence check, not an inference from a successful run.
3. **Cut the dispatcher over to it**, with the old query still present behind a setting.
4. **Observe one full dispatch cycle**: jobs claimed, no duplicates, no starvation. The existing
   invariants are the test — `jobs` count stable over a window, `job_attempts` progressing.
5. **Only then** consider revoking `BYPASSRLS` from `granada_fleet`.
6. **Then, and only then**, may a browser worker be issued credentials.

**Step 5 is deliberately last and is not required for step 6.** A browser worker can use
`granada_app` immediately; the dispatcher's role can be narrowed afterwards, or not at all.

## Rollback

```sql
DROP FUNCTION IF EXISTS fleet_due_job_ids(int);
```

Then flip the dispatcher's setting back to the direct query. **No data is at risk at any point**,
because this plan never modifies a table, a policy or a row — only adds a function nothing is forced
to call.

If `BYPASSRLS` has already been revoked at step 5 and something depended on it:

```sql
ALTER ROLE granada_fleet BYPASSRLS;
```

That restores the current state exactly.

## Restrictions, stated for the record

Before any live browser worker receives tenant credentials:

| Restriction | Enforced by |
|---|---|
| The browser worker's role must NOT have `BYPASSRLS` | use `granada_app`; assert it at startup |
| Discovery must return identifiers only | the function's return type |
| Tenant binding must be per-job and explicit | `require_org_access`, existing |
| Credentials and packages reachable only after the binding | job-scoped vault access |
| Revoking `BYPASSRLS` is not a prerequisite for a browser worker | it uses `granada_app` from the start |

## What this plan deliberately does not do

It does not replace RLS with application-level filtering, does not change table ownership, does not
redefine any existing policy, and does not revoke a privilege the running system currently depends on
in the same step that introduces the replacement. The directive requires compatibility tests, a
rollback plan and verification; this document is the plan, and steps 1–4 are the compatibility tests.
