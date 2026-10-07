-- Runtime role privileges for the Auth service.
--
-- Run as the schema owner (granada_user), against granada_auth, after every
-- migration that adds a table. Idempotent.
--
--   psql -U granada_user -d granada_auth -f sql/grant_runtime_role.sql
--
-- WHY THIS FILE EXISTS
-- --------------------
-- The application connects as `granada_app`, not as the owner. Two independent
-- reasons:
--
--   1. Row-level security. `organisations`, `roles`, `saml_providers`,
--      `user_contexts` and `audit_logs` are FORCE ROW LEVEL SECURITY, which
--      binds the table owner too. A connection as the owner would have every
--      tenant row visible regardless of the policies.
--   2. Blast radius. The owner can alter tables, drop them, and bypass every
--      policy. An injection bug in the service should not carry that.
--
-- The owner role is still needed - Alembic runs as it - so it stays, but it is
-- never the application's login.
--
-- WHAT IS DELIBERATELY NOT GRANTED
-- --------------------------------
-- * `alembic_version`. The runtime must not be able to declare the schema at a
--   revision it is not at; that is how a half-migrated database gets mistaken
--   for a healthy one.
-- * TRUNCATE, which bypasses row-level security entirely.
-- * REFERENCES and TRIGGER, which the service never needs.
-- * Any privilege on another database. CONNECT was revoked from PUBLIC during
--   the privilege split.

-- ---------------------------------------------------------------------------
-- Application tables
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA public
    TO granada_app;

-- ---------------------------------------------------------------------------
-- Withdraw what the blanket grant above just handed over
-- ---------------------------------------------------------------------------
REVOKE ALL ON TABLE alembic_version FROM granada_app;

-- Ledger and evidence tables: the runtime records, it does not erase.
--
-- This block exists because a GRANT is ADDITIVE. Migrations 004 and 005 grant
-- only SELECT/INSERT/UPDATE on `jobs`, `job_attempts` and `model_invocations`,
-- expressing the intent that the application may never delete the record of
-- work it is accountable for. The blanket grant above hands over DELETE
-- regardless, and granting DELETE after granting INSERT does not take it back -
-- so without these REVOKEs that intent was documented but not in force.
--
-- Wrapped in a DO block so the file stays idempotent and runnable against a
-- database that has not yet applied 004 or 005.
DO $$
DECLARE
    evidence_table text;
BEGIN
    FOREACH evidence_table IN ARRAY ARRAY['jobs', 'job_attempts', 'model_invocations']
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = evidence_table
        ) THEN
            EXECUTE format('REVOKE DELETE ON TABLE %I FROM granada_app', evidence_table);
        END IF;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Future tables created by later migrations
-- ---------------------------------------------------------------------------
-- Without this, every new table inherits privileges from PUBLIC only, and the
-- service breaks at the next migration with "permission denied for table X".
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO granada_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Sequences
-- ---------------------------------------------------------------------------
-- No table in granada_auth uses a serial or identity column today: every
-- primary key is a VARCHAR(36) filled by the application. The grant is kept so
-- that adding an identity column later does not silently break inserts.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO granada_app;

-- ---------------------------------------------------------------------------
-- Verification
-- ---------------------------------------------------------------------------
-- Run after applying. Expected: one row per application table (23 as of
-- revision 005), SELECT/INSERT/UPDATE everywhere, DELETE everywhere EXCEPT
-- alembic_version and the evidence tables (`jobs`, `job_attempts`,
-- `model_invocations`).
--
-- The DO block is what makes that expectation true, and it is asserted against
-- the live database with `has_table_privilege`, not from this view:
--
--   SELECT table_name,
--          string_agg(privilege_type, ',' ORDER BY privilege_type)
--     FROM information_schema.role_table_grants
--    WHERE grantee = 'granada_app'
--    GROUP BY table_name
--    ORDER BY table_name;
--
-- CAUTION: `information_schema.role_table_grants` is NOT authoritative here.
-- It was once observed attributing 7 privileges - including TRUNCATE - on
-- `alembic_version` to granada_app, while `has_table_privilege()` returned
-- False for all seven and a real connection was refused. The view reports
-- rows affected by role membership and grantor-side state, not necessarily
-- what the grantee can actually do. Trust this instead:
--
--   SELECT has_table_privilege('granada_app', 'alembic_version', 'SELECT'),
--          has_table_privilege('granada_app', 'jobs', 'DELETE'),
--          has_table_privilege('granada_app', 'model_invocations', 'DELETE');
--
-- Expected: f, f, f.