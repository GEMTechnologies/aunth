"""Resolve an inbound address to its organisation without a tenant binding.

Revision ID: 022_mail_address_lookup
Revises: 021_mail_verification

THE PROBLEM THIS SOLVES. `mail_identities` is protected by RLS (`org_id = app.current_org()`). The
inbound doorway must answer "whose mailbox is this?" BEFORE it knows the tenant - that is the
question - and with no binding `app.current_org()` is NULL, so the lookup returned zero rows and
every delivery was rejected as "no such mailbox". Measured live: a real verification email to a real
provisioned address was rejected.

A SECURITY DEFINER function answers exactly that one question and nothing else. It is deliberately
narrow: it returns the identity's org, agent and status for ONE address, never message content, never
a list, and never a wildcard. Bounded like this it is the same shape as the existing
`fleet_active_agent_ids()` in ADR-0011, and it is the only way an unauthenticated-by-tenant caller
can be told which tenant a mailbox belongs to.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "022_mail_address_lookup"
down_revision: Union[str, None] = "021_mail_verification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FUNCTION = "mail_org_for_address"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {FUNCTION}(p_address text)
        RETURNS TABLE (identity_id text, org_id text, agent_id text, identity_status text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        STABLE
        AS $$
            SELECT id::text, org_id::text, agent_id::text, status::text
            FROM mail_identities
            WHERE lower(address) = lower(p_address)
            LIMIT 1;
        $$;
        """
    )
    # Closed by default, then granted to the application role only. A function that can read across
    # tenants must not be executable by every role that happens to exist.
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION}(text) FROM PUBLIC")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT EXECUTE ON FUNCTION {FUNCTION}(text) TO granada_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_fleet') THEN
                GRANT EXECUTE ON FUNCTION {FUNCTION}(text) TO granada_fleet;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}(text)")
