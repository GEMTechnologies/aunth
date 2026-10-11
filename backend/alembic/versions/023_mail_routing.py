"""A routing table for inbound mail, kept in sync with mail_identities.

Revision ID: 023_mail_routing
Revises: 022_mail_address_lookup

WHY NOT THE SECURITY DEFINER FUNCTION. 022 created `mail_org_for_address` as SECURITY DEFINER, but
its owner is `granada_user`, not a superuser, so FORCE ROW LEVEL SECURITY still applied to it and it
returned zero rows - measured live: the function answered nothing for an address that existed. Making
it work would have meant either a role with BYPASSRLS (which ADR-0011 deliberately removed) or a
permissive policy on `mail_identities`, whose `token` column is the reply-alias secret. Neither is a
trade worth making for a lookup.

WHAT THIS DOES INSTEAD. Inbound routing needs to answer "whose mailbox is this?" before any tenant
context exists. That is a different question from "what is in this mailbox", so it gets a different
table: address, org, identity, active. No secrets, no message content, no tokens - and therefore no
RLS, because a table with nothing sensitive in it is not worth a policy that would only be worked
around.

Kept in sync by TRIGGER rather than by convention, so an identity created anywhere - onboarding, a
test, a migration - is routable immediately and cannot drift from its route.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "023_mail_routing"
down_revision: Union[str, None] = "022_mail_address_lookup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "mail_routing"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("address", sa.String(320), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("identity_id", sa.String(36), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_mail_routing_org", TABLE, ["org_id"])

    if not _is_postgres():
        return

    # BACKFILL: every mailbox that already exists becomes routable.
    op.execute(
        f"""
        INSERT INTO {TABLE} (address, org_id, identity_id, is_active)
        SELECT lower(address), org_id, id, (upper(coalesce(status, '')) = 'ACTIVE')
        FROM mail_identities
        ON CONFLICT (address) DO NOTHING
        """
    )

    # KEEP IN SYNC BY TRIGGER. A route maintained by application convention drifts the first time
    # somebody inserts an identity another way, and the symptom is mail silently rejected.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mail_routing_sync() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO mail_routing (address, org_id, identity_id, is_active)
            VALUES (lower(NEW.address), NEW.org_id, NEW.id,
                    (upper(coalesce(NEW.status, '')) = 'ACTIVE'))
            ON CONFLICT (address) DO UPDATE
                SET org_id = EXCLUDED.org_id,
                    identity_id = EXCLUDED.identity_id,
                    is_active = EXCLUDED.is_active;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        f"""
        DROP TRIGGER IF EXISTS trg_mail_routing_sync ON mail_identities;
        CREATE TRIGGER trg_mail_routing_sync
            AFTER INSERT OR UPDATE OF address, status, org_id ON mail_identities
            FOR EACH ROW EXECUTE FUNCTION mail_routing_sync();
        """
    )

    # Read-only for the application: this table is a lookup, never a thing the app writes directly.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT ON TABLE {TABLE} TO granada_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_fleet') THEN
                GRANT SELECT ON TABLE {TABLE} TO granada_fleet;
            END IF;
        END
        $$;
        """
    )

    # The function from 022 returned nothing in practice and is removed rather than left as a
    # misleading affordance that looks like it should work.
    op.execute("DROP FUNCTION IF EXISTS mail_org_for_address(text)")


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP TRIGGER IF EXISTS trg_mail_routing_sync ON mail_identities")
        op.execute("DROP FUNCTION IF EXISTS mail_routing_sync()")
    op.drop_table(TABLE)
