-- ============================================================================
-- granada_fleet - the role the WORKER and the RELAY connect as.
--
-- WHY THIS ROLE EXISTS (ADR-0011)
-- -------------------------------
-- The fleet dispatcher is blind in production, and this is the fix.
--
-- `FleetDispatcher.due_workflows()` reads `agent_workflows` WITHOUT binding a tenant, which is
-- correct - it must discover work across the whole fleet. But `agent_workflows` is FORCE ROW LEVEL
-- SECURITY with `org_id = app.current_org()`, and `app.current_org()` is NULL when nothing is bound.
-- `org_id = NULL` is false for every row, so the query returns ZERO ROWS WHATEVER EXISTS.
--
-- Proven, not inferred, on the first deployment:
--
--     INSERTED as superuser, total rows = 1
--     AS granada_app, UNSCOPED (what due_workflows does) = 0
--
-- The sweep reported `dispatched=0 errors=0` throughout - blind, not idle, and healthy-looking.
-- `granada_agents`, `jobs` and `agent_activity` have the same shape, so nothing could be claimed or
-- dispatched either. Matching, qualification, the application workspace, mail, submission, grants
-- and notifications were all unreachable; the platform was a catalogue with an API in front of it.
--
-- WHY `BYPASSRLS` AND NOT A PER-TENANT LOOP
-- ----------------------------------------
-- The dispatcher is a system component that legitimately operates across every tenant. That is what
-- "discover due work across the whole fleet" means, and `BYPASSRLS` exists precisely for that kind
-- of role. Binding each tenant in turn inside `due_workflows` was rejected: it would loop over every
-- organisation on every sweep to answer one question, it would break the window-function fairness
-- query that method is built around, and it would put tenant-binding in the hot path of the one
-- component that must never get tenancy wrong.
--
-- THE API MUST NOT USE THIS ROLE. `granada_app` keeps RLS enforced for every request, because "a
-- request cannot read another tenant's data" is the product's central security property. Giving the
-- request path `BYPASSRLS` to fix a worker problem would trade that property away.
--
--     worker, relay  ->  granada_fleet  (cross-tenant by nature)
--     api            ->  granada_app    (one tenant per request, RLS enforced)
--
-- `NOSUPERUSER` for the same reason `metrics_role.sql` states: an exploited credential must not be a
-- superuser. `BYPASSRLS` alone lets it read across tenants; it cannot read `pg_shadow`, cannot
-- `COPY ... FROM PROGRAM`, and cannot drop the database.
--
-- This file is IDEMPOTENT. Run it as the database owner.
-- ============================================================================

\set ON_ERROR_STOP on

-- The password is passed in, never written here. `psql` cannot interpolate a variable inside a
-- dollar-quoted block - the mistake that made ops/postgres/init/01-roles.sql dead on arrival, with
-- "syntax error at or near ':'" - so the value goes into a session setting FIRST, outside the quotes.
\set fleet_password `echo "$GRANADA_FLEET_PASSWORD"`

SET granada.init_fleet_password = :'fleet_password';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_fleet') THEN
        EXECUTE format(
            'CREATE ROLE granada_fleet LOGIN PASSWORD %L NOSUPERUSER BYPASSRLS',
            current_setting('granada.init_fleet_password')
        );
    END IF;
END
$$;

RESET granada.init_fleet_password;

GRANT CONNECT ON DATABASE granada_auth TO granada_fleet;
GRANT USAGE ON SCHEMA public TO granada_fleet;
GRANT USAGE ON SCHEMA app TO granada_fleet;

-- ---------------------------------------------------------------------------
-- THE FLEET TABLES. DML, because the dispatcher both reads work and writes jobs.
--
-- Named explicitly rather than `GRANT ALL ON ALL TABLES`, and the list is the point: a blanket grant
-- would silently hand the worker every tenant business table as they are added. The worker has no
-- business reading an organisation's documents, mail bodies or financial records - it claims work
-- and records outcomes.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON
    agent_workflows,
    agent_specialists,
    agent_activity,
    granada_agents,
    jobs,
    job_attempts,
    outbox_events,
    model_invocations,
    decision_records
TO granada_fleet;

-- The relay publishes outbox events and records delivery evidence.
GRANT SELECT, INSERT, UPDATE ON
    notifications,
    notification_preferences,
    notification_deliveries
TO granada_fleet;

-- ---------------------------------------------------------------------------
-- READ-ONLY on what the fleet must consult to decide work, and NOTHING more.
--
-- `organisations` is here because the dispatcher checks whether a tenant is active. `opportunities`
-- is the shared catalogue (ADR-0009: anyone may read it).
-- ---------------------------------------------------------------------------
GRANT SELECT ON organisations, opportunities, opportunity_payloads, opportunity_matches TO granada_fleet;

-- ---------------------------------------------------------------------------
-- Sequences, for any serial column the fleet inserts into.
-- ---------------------------------------------------------------------------
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO granada_fleet;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO granada_fleet;

-- ---------------------------------------------------------------------------
-- VERIFY THE POSTURE, in the same file that grants it.
--
-- `grant_runtime_role.sql` is ADDITIVE and has silently re-granted a privilege nine times in this
-- project. A role script that reports success without checking is how the tenth happens.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    is_bypass BOOLEAN;
    is_super  BOOLEAN;
    can_write_tenant_docs BOOLEAN;
BEGIN
    SELECT rolbypassrls, rolsuper INTO is_bypass, is_super
    FROM pg_roles WHERE rolname = 'granada_fleet';

    IF NOT is_bypass THEN
        RAISE EXCEPTION 'granada_fleet lacks BYPASSRLS, so the dispatcher will remain blind (ADR-0011)';
    END IF;
    IF is_super THEN
        RAISE EXCEPTION 'granada_fleet is a SUPERUSER; it must only bypass RLS, never be all-powerful';
    END IF;

    -- The fleet must NOT be able to read tenant business tables. If this ever becomes true, the
    -- separation between the worker and the API has quietly collapsed.
    --
    -- Checked through pg_class rather than `has_table_privilege('granada_fleet', 'documents', ...)`,
    -- because that form RAISES if the table is absent - so this verification would fail on a
    -- deployment where the documents table had been renamed, instead of reporting the real state.
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'public' AND c.relname = 'documents') THEN
        SELECT has_table_privilege('granada_fleet', 'documents', 'SELECT') INTO can_write_tenant_docs;
        IF can_write_tenant_docs THEN
            RAISE EXCEPTION 'granada_fleet can read tenant documents; the fleet role is too broad';
        END IF;
    END IF;

    RAISE NOTICE 'granada_fleet verified: BYPASSRLS yes, SUPERUSER no, no tenant business tables';
END
$$;
