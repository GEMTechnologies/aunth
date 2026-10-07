-- Backup role for Granada.
--
-- WHY THIS IS NEEDED, AND WHY IT IS NOT OPTIONAL
-- ==============================================
-- `pg_dump` running as the application owner CANNOT read Granada's data:
--
--     pg_dump: error: query failed: ERROR: query would be affected by
--     row-level security policy for table "organisations"
--
-- FORCE row-level security binds the table OWNER as well as ordinary roles, which is
-- the whole point of FORCE - it means a maintenance script cannot quietly read across
-- tenants. `pg_dump` needs to read every tenant, so it needs a role that is explicitly
-- exempt: BYPASSRLS.
--
-- This is not a defect in Granada and it is not something to work around by weakening
-- the policies. It is a deployment prerequisite, and it is recorded here because
-- running the backup procedure is what revealed it - a written procedure would have
-- said "run pg_dump" and nobody would have learned this until a restore was needed.
--
-- Verified working: `pg_dump --schema-only` as the application owner produces a
-- COMPLETE artefact - every table, every composite key and every RLS policy - so
-- schema-only backups need no privileged role. Only the DATA needs this one.
--
-- Run as a superuser (e.g. postgres):

CREATE ROLE granada_backup LOGIN PASSWORD '<a strong secret>' BYPASSRLS;

GRANT CONNECT ON DATABASE granada_auth TO granada_backup;
GRANT USAGE ON SCHEMA public TO granada_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO granada_backup;

-- So tables created by later migrations are readable without re-granting.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO granada_backup;

-- Restore verification creates a scratch database, so it needs CREATEDB. Omit this if
-- you verify restores elsewhere; the backup itself does not need it.
ALTER ROLE granada_backup CREATEDB;

-- ---------------------------------------------------------------------------
-- Then:
-- ---------------------------------------------------------------------------
--   pg_dump -U granada_backup -Fc -f granada-$(date +%F).dump granada_auth
--   pg_restore -U granada_backup -d granada_auth_restored granada-$(date +%F).dump
--
-- Verify the artefact before trusting it:
--   python tools/backup_restore_check.py --admin-url "$GRANADA_ADMIN_DATABASE_URL"
--
-- ---------------------------------------------------------------------------
-- DO NOT DO THIS INSTEAD
-- ---------------------------------------------------------------------------
--   ALTER TABLE ... NO FORCE ROW LEVEL SECURITY;
--   pg_dump ...
--   ALTER TABLE ... FORCE ROW LEVEL SECURITY;
--
-- It appears to work and it disables the tenant boundary for the duration of the
-- backup - precisely when an operator is least likely to be watching. Worse, a failure
-- to re-enable it is SILENT: every subsequent query still succeeds, and the database
-- simply stops isolating tenants. A privileged backup role is the supported answer.

-- ---------------------------------------------------------------------------
-- WHAT A BACKUP MUST NOT CARRY
-- ---------------------------------------------------------------------------
-- The dump must contain ONLY the `public` and `app` schemas. `app` holds the RLS
-- helper functions (app.current_org, app.current_user_id, app.user_org_ids) and is
-- required. Anything else is a stray.
--
-- A leftover `rls_test_<hex>` schema was found in the live database during Phase 10,
-- created by the RLS test suite and left behind when a test run was killed - a
-- `finally` block cannot run when the process is killed. It travelled into every
-- backup. `tools/backup_restore_check.py` now fails when it sees an unexpected schema,
-- and this cleans one up:
--
--   DROP SCHEMA IF EXISTS rls_test_<hex> CASCADE;
--
-- Find them with:
--   SELECT schema_name FROM information_schema.schemata
--    WHERE schema_name ~ '^rls_test_[0-9a-f]{10}$';
