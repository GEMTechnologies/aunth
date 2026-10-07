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

-- Ledger, evidence, identity and history tables: the runtime records, it does
-- not erase.
--
-- This block exists because a GRANT is ADDITIVE. Migrations 004, 005, 006, 009
-- and 010 grant only the privileges each table needs, expressing the intent that
-- the application may never delete the record of what it did, nor the
-- organisation's own facts, documents and application history. The blanket grant
-- above hands over DELETE regardless, and granting DELETE after granting INSERT
-- does not take it back - so without these REVOKEs that intent was documented but
-- not in force.
--
-- `application_transitions` is the sharpest case: it is an append-only audit
-- trail, and an audit trail that its own subject can rewrite or erase is not an
-- audit trail. Migration 010 grants only SELECT and INSERT on it, and this
-- REVOKE is what makes that true rather than aspirational.
--
-- `org_facts` and `documents` are on the list for a further reason: their
-- history IS the product. Superseding a fact is an UPDATE (`is_current` goes
-- false), and that only works if the old row survives. A runtime role able to
-- DELETE would let an agent erase the version an application was submitted
-- against, which is precisely what the "Why?" evidence view depends on.
--
-- Wrapped in a DO block so the file stays idempotent and runnable against a
-- database that has not yet applied every migration.
DO $$
DECLARE
    evidence_table text;
BEGIN
    FOREACH evidence_table IN ARRAY ARRAY[
        'jobs', 'job_attempts', 'model_invocations', 'org_facts', 'documents',
        'decision_records', 'application_transitions', 'agent_activity',
        'donor_research',
        -- Phase 7b outbound history. Added here as well as in migration 014,
        -- because THIS script is additive and is what gets re-run: without these
        -- entries a future run would silently hand back UPDATE and DELETE on the
        -- append-only send history, which is the sixth occurrence of a trap that has
        -- already bitten jobs, model_invocations, decision_records,
        -- application_transitions, agent_activity and donor_research.
        'mail_send_attempts', 'mail_approvals',
        -- DELETE is withheld but UPDATE is NOT: a send intent's status must advance
        -- from WAITING_FOR_APPROVAL to APPROVED to SENDING to SENT. Its HISTORY is
        -- append-only; its LIFECYCLE is not.
        --
        -- This entry exists because leaving it out re-granted DELETE the moment the
        -- script was re-run. It was found by verifying the posture AFTER applying the
        -- script rather than trusting that migration 014's REVOKE still stood - which
        -- is the only way this trap has ever been caught. It had caught seven tables
        -- before Phase 8 (jobs, model_invocations, decision_records,
        -- application_transitions, agent_activity, donor_research, mail_send_intents);
        -- the submission entries below were added BEFORE the script was next run,
        -- which is the first time this trap has been closed ahead of time rather than
        -- after the fact.
        'mail_send_intents',
        -- Phase 8 submission history. Added here as well as in migration 016, for the
        -- same reason as every entry above: THIS script is additive and is what gets
        -- re-run, so leaving them out would hand back UPDATE and DELETE on the record of
        -- which applications were filed and what the funder acknowledged.
        --
        -- Found by `tools/security_scan.py`, which checks structurally that every
        -- append-only table appears in this file - because the way this trap has always
        -- been caught before is by verifying the posture AFTER applying the script, and
        -- that only catches it once it has already happened.
        --
        -- `submission_receipts` is the sharper of the two. A receipt is the EVIDENCE that
        -- a funder received an application; one its own subject can rewrite is not
        -- evidence, and `SUBMITTED` is only ever recorded against a receipt.
        'submission_attempts', 'submission_receipts',
        -- DELETE is revoked but UPDATE is NOT. A package's status must advance from DRAFT
        -- to AWAITING_AUTHORISATION to AUTHORISED to SUBMITTING to SUBMITTED, so UPDATE
        -- belongs to it. DELETING it does not: a package is the record of which documents
        -- a person authorised and what was filed, and a record its own subject can erase
        -- is not a record.
        --
        -- This entry was ADDED AFTER the script had already been re-run, because the first
        -- version of this fix named the two history tables and stopped there. Verifying the
        -- posture afterwards showed `submission_packages` had gone from D=false to D=true -
        -- the broad grant at line 37 standing unrevoked. That is the ninth time this trap
        -- has fired, and the first time it was caught by a check rather than by a report.
        'submission_packages'
    ]
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = evidence_table
        ) THEN
            EXECUTE format('REVOKE DELETE ON TABLE %I FROM granada_app', evidence_table);
            -- Append-only tables must not be editable either. A history or an
            -- evidence record a caller can rewrite is worse than none, because it
            -- looks authoritative.
            -- `submission_packages` is deliberately NOT in THIS list, while being IN the
            -- DELETE list above. Its status must advance from DRAFT to
            -- AWAITING_AUTHORISATION to AUTHORISED to SUBMITTING to SUBMITTED, so UPDATE
            -- belongs to it. Its LIFECYCLE is mutable; its EXISTENCE is not. The same
            -- distinction as `mail_send_intents`.
            IF evidence_table IN (
                'application_transitions', 'agent_activity', 'donor_research',
                'mail_send_attempts', 'mail_approvals',
                'submission_attempts', 'submission_receipts'
            ) THEN
                EXECUTE format('REVOKE UPDATE ON TABLE %I FROM granada_app', evidence_table);
            END IF;
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
--          has_table_privilege('granada_app', 'model_invocations', 'DELETE'),
--          has_table_privilege('granada_app', 'submission_attempts', 'UPDATE'),
--          has_table_privilege('granada_app', 'submission_receipts', 'UPDATE'),
--          has_table_privilege('granada_app', 'submission_packages', 'UPDATE'),
--          has_table_privilege('granada_app', 'submission_packages', 'DELETE');
--
-- Expected: f, f, f, f, f, t, f  - UPDATE on a package is TRUE because its status must
-- advance, and DELETE on it is FALSE because its existence is the record. A blanket
-- `f, f, f, f, f, f, f` would mean UPDATE had been over-revoked and no application could
-- ever leave DRAFT; an all-`t` tail would mean the broad grant at line 37 was standing.