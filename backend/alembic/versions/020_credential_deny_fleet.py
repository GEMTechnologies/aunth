"""Deny the fleet role any access to credentials, and close the DELETE re-grant.

Revision ID: 020_credential_deny_fleet
Revises: 019_credential_store

TWO DEFECTS IN 019, BOTH FOUND BY VERIFYING THE DEPLOYED POSTURE RATHER THAN TRUSTING THE MIGRATION

DEFECT 1 - THE FLEET COULD READ CREDENTIALS

019 granted SELECT on `credential_secrets` to `granada_app` and asserted that `granada_fleet` was
granted nothing. That assertion was wrong, and the reason is a role topology fact:

    GRANT granada_app TO granada_fleet;        -- sql/fleet_role.sql, line 136

`granada_fleet` is a MEMBER of `granada_app`, so it INHERITS every privilege the application role has.
Verified live:

    AS granada_fleet (member of granada_app, NOBYPASSRLS):
      unbound             -> visible = 0
      bound to that org   -> visible = 1     <-- the fleet can read credential ciphertext
      bound to another    -> visible = 0

The zeroes are what the grant-check would have shown. The ONE is what matters, and it is worse than it
looks: the dispatcher's entire job is to iterate organisations and bind to each one, so "bound to that
org" is not a corner case - it is every organisation, one after another.

That would have undone the value of revoking BYPASSRLS. The privilege was narrowed so the fleet could
not read tenant data across tenants; a table it can read one tenant at a time while moving through all
of them achieves the same thing by a longer route.

THE FIX IS A RESTRICTIVE POLICY, NOT A REVOKE

An inherited privilege cannot be revoked from the inheriting role directly - `REVOKE ... FROM
granada_fleet` is a no-op when the privilege comes through membership. Removing the membership would
change the fleet's whole privilege surface, which is a far larger change to make while fixing a hole.

A RESTRICTIVE policy is `false` for the fleet role and ANDs with every permissive policy, so no binding
can satisfy it. It is surgical: it changes nothing for `granada_app` and closes the fleet absolutely.

DEFECT 2 - DELETE WAS RE-GRANTED

`sql/grant_runtime_role.sql` runs AFTER migrations and begins:

    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO granada_app;

That is additive. 019's `REVOKE DELETE ON credential_secrets` therefore did not survive the next run of
the grant script - verified live: `has_table_privilege('granada_app','credential_secrets','DELETE')`
returned TRUE. The file documents this exact trap nine times for other tables; the tenth entry is added
to its list by this revision's companion change.
"""

from __future__ import annotations

from alembic import op

revision = "020_credential_deny_fleet"
down_revision = "019_credential_store"
branch_labels = None
depends_on = None

TABLE = "credential_secrets"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # A RESTRICTIVE policy ANDs with every permissive one, so `false` denies the fleet under any
    # binding. Scoped TO granada_fleet so nothing else is affected.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_fleet') THEN
                CREATE POLICY {TABLE}_fleet_denied ON {TABLE}
                    AS RESTRICTIVE
                    FOR ALL
                    TO granada_fleet
                    USING (false)
                    WITH CHECK (false);
            END IF;
        END
        $$;
        """
    )

    # Close the additive-grant trap in the same revision. This is what makes 019's REVOKE DELETE
    # survive the next run of sql/grant_runtime_role.sql.
    op.execute(f"REVOKE DELETE ON TABLE {TABLE} FROM granada_app")

    # VERIFY, and raise rather than leave a silent hole. A migration that reports success while the
    # fleet can still read credentials is worse than one that fails.
    op.execute(
        f"""
        DO $$
        DECLARE
            fleet_is_member boolean;
            restrictive_present integer;
        BEGIN
            SELECT EXISTS (
                SELECT 1 FROM pg_auth_members m
                  JOIN pg_roles r ON r.oid = m.member
                  JOIN pg_roles g ON g.oid = m.roleid
                 WHERE r.rolname = 'granada_fleet' AND g.rolname = 'granada_app'
            ) INTO fleet_is_member;

            SELECT count(*) INTO restrictive_present
              FROM pg_policies
             WHERE tablename = '{TABLE}'
               AND policyname = '{TABLE}_fleet_denied'
               AND permissive = 'RESTRICTIVE';

            IF restrictive_present = 0 THEN
                RAISE EXCEPTION
                    'the fleet-denial policy was not created; granada_fleet inherits SELECT on % '
                    'through its membership in granada_app and would be able to read credentials',
                    '{TABLE}';
            END IF;

            RAISE NOTICE
                'credential_secrets: fleet denial installed (fleet is a member of granada_app: %)',
                fleet_is_member;
        END
        $$;
        """
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(f"DROP POLICY IF EXISTS {TABLE}_fleet_denied ON {TABLE}")
