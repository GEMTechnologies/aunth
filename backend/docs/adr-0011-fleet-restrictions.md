# ADR-0011: what is narrowed, what is not, and the restrictions required before a browser worker gets credentials

**Status:** candidate selection narrowed and proven; **`BYPASSRLS` remains granted**; remaining scope
enumerated
**Date:** 2026-10-10
**Supersedes:** the target-table error in `adr-0011-narrowing-plan.md` (see its top section)

## Where the privilege actually is

```
granada_fleet   bypassrls = TRUE    the only role with unfiltered cross-tenant visibility
granada_app     bypassrls = FALSE
granada_user    bypassrls = FALSE
```

Three services receive the fleet credential (`worker`, `relay`, and one more), all through
`FLEET_DATABASE_URL`. The API does **not**: giving the request path `BYPASSRLS` would remove the
guarantee that a request cannot read another tenant's data, in order to fix a worker problem.

## What IS narrowed

`fleet_due_workflow_ids(batch_size, per_agent_limit)` — `SECURITY DEFINER`, `STABLE`, **ids only**, over
`agent_workflows`, preserving the per-agent fairness partition:

```sql
row_number() OVER (PARTITION BY agent_id ORDER BY priority, next_run_at, id)
```

**The fairness partition is the load-bearing part.** The dispatcher's own comment records why: *"an
organisation with thousands of high-priority due workflows fills the entire window, and a smaller
organisation's work is never even fetched."* A narrow function returning due rows without the partition
would pass a small-fixture test and starve small organisations in production.

The dispatcher uses it when `FLEET_NARROW_CLAIM=1` (configuration, read at construction).

**Verified, not asserted:**

| Check | Evidence |
|---|---|
| Equivalence, forced non-empty set | `PROBE EQUIVALENCE = IDENTICAL`, rolled back |
| Equivalence, **live production data** | `function = 0`, `direct = 0`, `due now = 0` — both correct |
| Flag reaches the process | `printenv FLEET_NARROW_CLAIM` → `1` |
| Dispatcher still dispatches | jobs 1330 → 1380 |
| Fleet starts clean | `fleet.started`, no errors |

## What is NOT narrowed — enumerated, not estimated

**30 cross-tenant access sites in `agent/workflow_engine.py`**, across nine models:

```
AgentWorkflow · GranadaAgent · Opportunity · OpportunityMatch · Organisation
MailSendIntent · MailAccount · Application · SubmissionPackage
```

plus writes (`self.db.add`) at lines 863 and 946.

An earlier report said "three reads remain". **That was wrong by roughly 10×.** The dispatcher's
cross-tenant footprint is the whole engine, and the number matters because it is the difference between
a round and a project.

## Why the revoke is withheld

`BYPASSRLS` is not a convenience here — it is load-bearing for the other 30 sites. **Revoking it after
narrowing one query would break discovery, agent lookup, and every workflow write.** The privilege stays
granted, and §12 stays open.

## THE RESTRICTIONS REQUIRED BEFORE A LIVE BROWSER WORKER RECEIVES TENANT CREDENTIALS

This is the directive's own fallback, and it is stated here so it can be enforced rather than remembered.

1. **The worker must not hold `FLEET_DATABASE_URL`.** Enforced in code today
   (`agent/worker_privileges.py`), verified live: a worker with that variable set refuses before opening
   a browser.

2. **The worker must receive its task as data, never as a database connection.** It already does: the
   task arrives as JSON on stdin, and `BrowserTask.credentials` holds `CredentialRef` pointers, not
   secrets.

3. **No tenant document path may be resolvable by the worker except the ones named in its task.**
   `validate_task` refuses a `document_id` that does not belong to the task's organisation, and the
   evidence store is tenant-scoped by path.

4. **The worker's own environment must carry no secret it does not need.** Today it holds one portal
   credential pair, and the outcome scrubber removes both from anything it emits.

5. **A live worker must not run while `browser_execution_enabled` is 0.** The flag is the only thing
   preventing the browser path from being entered, and it is currently off.

6. **The 30 sites above are the dispatcher's problem, not the worker's.** Narrowing them is required to
   revoke `BYPASSRLS`; it is NOT required to give a worker a credential, because the worker never
   touches those tables. **These are two conditions, and conflating them is how a fleet revokes a
   privilege it still needs.**

## What completing the redesign would take

- a `SECURITY DEFINER` capability for each **distinct** cross-tenant need, not one per call site
- an equivalence proof for each, **over a non-empty set** — two empty sets are always equal, and this
  directive has already reported `IDENTICAL` over nothing
- a design for the **writes**, which a read-scoping function cannot cover
- and a session with room to check its own work: this directive has produced a false-pass hash check, a
  false alarm about a production tree, and a flag that "deployed" without arriving — all in one day

## ADDENDUM: the 30 call sites are not 30 capabilities

**Read rather than counted.** Taking `discover_opportunity_work` (the largest of the 30) and classifying
each statement:

```python
agents = SELECT * FROM granada_agents WHERE status = 'ACTIVE'   # CROSS-TENANT - the roster
for agent in agents:
    evaluated  = OpportunityMatch   WHERE org_id  == agent.org_id   # already tenant-scoped
    queued     = AgentWorkflow      WHERE agent_id == agent.id      # already tenant-scoped
    candidates = Opportunity        WHERE id NOT IN (...)           # the catalogue, unscoped by ADR-0009
    GranadaAgentService(self.db, agent.org_id)                      # already TENANT BOUND
```

**One statement needs cross-tenant visibility. Four do not.**

That matters because the shape of the fix changes: this method does not need a function per query, it
needs **one** narrow capability — *the active agent roster*, returning `id` and `org_id` only — after
which the loop can run under each agent's own tenant context.

**The same classification has not yet been done for the other 29 sites**, and it should be, because:

- if the ratio holds, the real capability count may be **a handful, not thirty**
- a per-call-site function would over-build and create thirty privileges where three would do
- **and "30 sites" as a plan understates nothing but overstates the work** — the opposite error to the
  "three reads" I reported two rounds earlier

**Both estimates were made from counting rather than reading.** The count is a starting point; the
classification is the plan.

