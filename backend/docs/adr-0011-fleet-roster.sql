-- ADR-0011 step 1: the ONLY genuinely cross-tenant read in the dispatcher.
--
-- WHY THIS EXISTS
--
-- The fleet dispatcher sweeps every organisation's due work. To do that it must first learn WHICH
-- organisations are active - a question no single tenant can answer, because the answer is the list of
-- tenants. That is the one read that legitimately crosses the tenant boundary.
--
-- Everything downstream does NOT: `discover_opportunity_work` already carries `agent.org_id` on every
-- query and constructs every service with an org. Verified against the live database (2026-10-10):
--
--   SET ROLE granada_app;                    -- rolbypassrls = FALSE
--     unbound            -> agent_workflows = 0        (blind: the "healthy-looking" failure)
--     app.current_org_id = <real org>  -> agent_workflows = 57
--     app.current_org_id = <other org> -> agent_workflows = 0        (isolation holds)
--
-- So the fleet role needs a ROSTER, not a privilege. This function returns ids and org ids. Nothing
-- else - not names, not settings, not credentials, not documents. A leaked roster is a list of who
-- exists; it is not tenant data.
--
-- WHY NOT JUST GRANT SELECT ON granada_agents
--
-- That would expose every column of every organisation's agent record - configuration, quotas,
-- throttle state, and whatever is added next - and it would keep the privilege attached to the
-- connection rather than to the question. A function returns what was asked for and nothing else.
--
-- WHY IT IS SECURITY DEFINER, AND WHY THAT IS SAFE HERE
--
-- It must read past RLS to see all tenants. That is the point. The safety comes from the shape of the
-- return value: two columns, no free-text, no joins to anything sensitive, no parameters that could be
-- used to select a different set of rows. There is no `WHERE` clause a caller can influence.

CREATE OR REPLACE FUNCTION fleet_active_agent_ids()
RETURNS TABLE(agent_id varchar, org_id varchar)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $function$
    SELECT a.id, a.org_id
    FROM granada_agents a
    WHERE a.status = 'ACTIVE'
    ORDER BY a.org_id, a.id
$function$;

COMMENT ON FUNCTION fleet_active_agent_ids() IS
    'ADR-0011: the dispatcher''s one cross-tenant read - which organisations are active. '
    'Returns ids only. Replaces the need for BYPASSRLS on the sweep.';

-- Same discipline as fleet_due_workflow_ids: nobody gets EXECUTE by default.
REVOKE ALL ON FUNCTION fleet_active_agent_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fleet_active_agent_ids() TO granada_fleet;

-- VERIFY (run as postgres, then repeat as granada_fleet):
--
--   SELECT count(*) FROM fleet_active_agent_ids();          -- expect 1
--   SET ROLE granada_fleet;
--   SELECT * FROM fleet_active_agent_ids();                 -- expect the roster
--   SET ROLE granada_app;
--   SELECT * FROM fleet_active_agent_ids();                 -- expect permission denied
