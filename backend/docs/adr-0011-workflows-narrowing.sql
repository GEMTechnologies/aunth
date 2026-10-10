-- ADR-0011 narrowing, CORRECTED: the privileged read is on agent_workflows, not jobs.
--
-- WHAT THE FIRST PLAN GOT WRONG
-- -----------------------------
-- `docs/adr-0011-narrowing-plan.md` narrowed `jobs` via `fleet_due_job_ids()`. Read before cutting
-- over, `workflow_engine.FleetDispatcher.dispatch_once` does:
--
--     candidates = self.due_workflows(now=moment, limit=limit)
--
-- and `due_workflows` reads `agent_workflows`. So the function the plan names is not a function the
-- dispatcher's privileged read would ever call. Applying that cutover would have left the real
-- cross-tenant read running under BYPASSRLS while appearing to close §12.
--
-- THE FAIRNESS GUARANTEE IS THE HARD PART, NOT THE FILTER
-- ------------------------------------------------------
-- `due_workflows` does not just select due rows. It ranks WITHIN each agent first:
--
--     row_number() OVER (PARTITION BY agent_id ORDER BY priority, next_run_at, id)
--
-- and the code says why: "The first version of this method fetched the global top-N by priority and
-- then capped per agent, which does not provide fairness at all: an organisation with thousands of
-- high-priority due workflows fills the entire window, and a smaller organisation's work is never
-- even fetched."
--
-- So a narrow function that returns due rows would REGRESS FAIRNESS - a correctness bug the privilege
-- review would not catch, and one that silently starves small organisations. The partition must move
-- into the function intact, along with the trailing `id` in the ORDER BY, which exists so two
-- concurrent dispatchers see the same window.
--
-- The function returns IDS ONLY. That is the narrowing: the dispatcher gets the same candidate set,
-- and everything it then does with those ids - reading the agent, the workflow's payload - happens
-- under the caller's own row policies, so a worker cannot use this to read another tenant's data.

BEGIN;

CREATE OR REPLACE FUNCTION fleet_due_workflow_ids(
    batch_size integer DEFAULT 200,
    per_agent_limit integer DEFAULT 25
)
RETURNS TABLE (workflow_id character varying)
LANGUAGE sql
STABLE
SECURITY DEFINER
-- OWNED BY the schema owner, so it runs with enough rights to see across tenants - which is the
-- whole point, and the whole risk. It is deliberately:
--   * SECURITY DEFINER, not a grant of BYPASSRLS: the privilege is scoped to this query.
--   * STABLE, not VOLATILE: it must not be used to write.
--   * IDS ONLY: no tenant columns, no payload, nothing a caller could exfiltrate.
SET search_path = public, pg_catalog
AS $$
    WITH ranked AS (
        SELECT
            w.id,
            row_number() OVER (
                PARTITION BY w.agent_id
                ORDER BY w.priority ASC, w.next_run_at ASC, w.id ASC
            ) AS agent_rank,
            w.priority,
            w.next_run_at,
            w.id AS tiebreak
        FROM agent_workflows w
        WHERE w.state IN ('PENDING', 'WAITING')
          AND w.next_run_at IS NOT NULL
          AND w.next_run_at <= now()
    )
    SELECT r.id AS workflow_id
    FROM ranked r
    WHERE r.agent_rank <= per_agent_limit
    -- The global ordering after partitioning: priority, then due time, then id. The trailing id is
    -- REQUIRED, not decorative - without a total order two dispatchers see different windows.
    ORDER BY r.priority ASC, r.next_run_at ASC, r.tiebreak ASC
    LIMIT batch_size;
$$;

-- Only the fleet role may call it. Executing this function is the capability; nothing else changes.
REVOKE ALL ON FUNCTION fleet_due_workflow_ids(integer, integer) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_fleet') THEN
        GRANT EXECUTE ON FUNCTION fleet_due_workflow_ids(integer, integer) TO granada_fleet;
    END IF;
END $$;

-- NOTE: `fleet_due_job_ids` (the jobs-targeted function from the first plan) is NOT dropped here.
-- It is additive and harmless, and removing it in the same migration that adds its replacement would
-- mix two changes. `jobs` IS also read cross-tenant by parts of the fleet and may need its own
-- narrowing - that is the SECOND question, and this migration deliberately answers only the first.

COMMIT;

-- EQUIVALENCE CHECK - run before cutting over, and require both counts to be NON-ZERO:
--
--   SELECT 'function' AS path, count(*) FROM fleet_due_workflow_ids(200, 25)
--   UNION ALL
--   SELECT 'direct', count(*) FROM (
--       SELECT w.id FROM (
--         SELECT w.id, row_number() OVER (
--             PARTITION BY w.agent_id ORDER BY w.priority, w.next_run_at, w.id) AS r
--         FROM agent_workflows w
--         WHERE w.state IN ('PENDING','WAITING') AND w.next_run_at IS NOT NULL
--           AND w.next_run_at <= now()
--       ) w WHERE w.r <= 25
--       ORDER BY w.id LIMIT 200
--   ) x;
--
-- An equivalence check over an EMPTY set proves nothing: two empty sets are always equal. This is
-- exactly the mistake made earlier in this directive, so the check must be run when work IS due, or
-- forced inside a transaction that is rolled back - as was done for the jobs function.
