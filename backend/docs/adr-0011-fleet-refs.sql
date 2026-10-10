-- ADR-0011 step 2, part 1: the candidate refs must carry their tenant.
--
-- WHY A SECOND FUNCTION RATHER THAN CHANGING THE FIRST
--
-- `fleet_due_workflow_ids()` is already deployed and is what `FLEET_NARROW_CLAIM=1` calls. Changing
-- its return type would break the running dispatcher at deploy time. This adds a sibling and leaves
-- the old one in place, so the cutover to tenant binding is a separate, reversible decision.
--
-- WHY org_id IS NEEDED AT ALL - the finding that forced this
--
-- The narrow path does two steps:
--
--   1. ids from the function          -- SECURITY DEFINER, crosses tenants by design
--   2. rows from the ORM by those ids -- `SELECT ... WHERE id IN (...)` under the CALLER's policies
--
-- Step 2 is the problem. Those ids belong to MANY tenants, so there is no single value of
-- `app.current_org_id` that makes the re-select work once `granada_fleet` loses BYPASSRLS: bound to
-- org A it returns A's rows only, and the fleet silently dispatches one organisation per sweep.
-- Bound to nothing it returns zero - the "healthy-looking and blind" failure.
--
-- The dispatcher therefore cannot re-select across tenants at all. It must process ONE WORKFLOW AT A
-- TIME, bound to that workflow's own organisation. To do that it has to know the organisation before
-- it loads the row - which is what this function provides.
--
-- WHY THIS IS STILL NARROW
--
-- `org_id` is an identifier, not tenant data. The return shape stays two varchar columns with no
-- free text, no joins to anything sensitive, and no caller-influenced WHERE clause. A leaked list of
-- (workflow id, organisation id) pairs says which work exists; it does not disclose a single field of
-- any organisation's records. The privilege remains attached to the question "what is due and whose
-- is it", which is exactly the question the dispatcher must ask.
--
-- The fairness partition is preserved verbatim from the original. It is load-bearing: without it an
-- organisation with thousands of due workflows fills the whole window and a smaller organisation's
-- work is never fetched.

CREATE OR REPLACE FUNCTION fleet_due_workflow_refs(
    batch_size integer DEFAULT 200,
    per_agent_limit integer DEFAULT 25
)
RETURNS TABLE(workflow_id varchar, org_id varchar)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $function$
    WITH ranked AS (
        SELECT
            w.id,
            w.org_id,
            row_number() OVER (
                PARTITION BY w.agent_id
                ORDER BY w.priority, w.next_run_at, w.id
            ) AS agent_rank
        FROM agent_workflows w
        WHERE w.state IN ('PENDING', 'WAITING')
          AND w.next_run_at IS NOT NULL
          AND w.next_run_at <= now()
    )
    SELECT r.id, r.org_id
    FROM ranked r
    WHERE r.agent_rank <= per_agent_limit
    ORDER BY r.id
    LIMIT batch_size
$function$;

COMMENT ON FUNCTION fleet_due_workflow_refs(integer, integer) IS
    'ADR-0011: due workflow ids WITH their organisation, so the dispatcher can process one tenant at '
    'a time without BYPASSRLS. Two id columns only; preserves the per-agent fairness partition.';

REVOKE ALL ON FUNCTION fleet_due_workflow_refs(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fleet_due_workflow_refs(integer, integer) TO granada_fleet;

-- VERIFY:
--   SELECT 'refs = ' || count(*) FROM fleet_due_workflow_refs(200, 25);
--   SELECT DISTINCT org_id FROM fleet_due_workflow_refs(200, 25);   -- must be non-empty when due work exists
--   SET ROLE granada_app;
--   SELECT count(*) FROM fleet_due_workflow_refs(200, 25);          -- expect permission denied
