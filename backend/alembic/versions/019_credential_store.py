"""The credential store: encrypted per-organisation secrets.

Revision ID: 019_credential_store
Revises: 018_notifications

WHY THIS MIGRATION EXISTS

`MailAccount.credentials_ref` has pointed at a secret store since 013, and no store existed. So an
OAuth access token had nowhere to live and a per-organisation IMAP password was impossible by
construction - mail could be configured for a deployment and never for an organisation.

THE SCHEMA'S RULE IS NOT WEAKENED

    "No provider password is ever stored. There is no column for one, and `credentials_ref` points at
     the secret store rather than holding a secret."

`ciphertext` is a Fernet token - authenticated encryption, useless without a key that lives in the
environment and never in the database. There is no plaintext column, no key column, and no column
recording what a credential is worth. A dump of this table discloses WHICH organisations have
connections, which is metadata rather than a credential.

RLS IS FORCED, LIKE EVERY OTHER ORGANISATION-OWNED TABLE

FORCE matters more here than anywhere else: it binds the table owner, so a migration or a maintenance
script cannot quietly read every organisation's ciphertext.

DELETE IS WITHHELD FROM THE RUNTIME ROLE

A credential is revoked by destroying its ciphertext, not by deleting the row. Keeping the row means
"when did this connection stop working" has an answer after the fact, and a runtime role that cannot
delete cannot make that evidence disappear.

`granada_fleet` IS NOT GRANTED ANYTHING

The fleet dispatcher has no business reading credentials: it schedules work by organisation id. Its
only cross-tenant read is the roster, and this table is deliberately outside that. With BYPASSRLS now
revoked, its absence is enforced by RLS as well as by the grant.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "019_credential_store"
down_revision = "018_notifications"
branch_labels = None
depends_on = None

TABLE = "credential_secrets"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        # The name a caller resolves by. Not a secret; unique only within one organisation.
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        # A Fernet token, not a password. Useless without the environment key.
        sa.Column("ciphertext", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
        # Operator-facing free text: "token expired", "revoked by the customer". Never the secret.
        sa.Column("note", sa.String(500)),
        sa.UniqueConstraint("org_id", "ref", name="uq_credential_secret_org_ref"),
    )
    op.create_index("ix_credential_secrets_org_id", TABLE, ["org_id"])
    op.create_index("ix_credential_secrets_ref", TABLE, ["ref"])
    op.create_index("ix_credential_secrets_status", TABLE, ["status"])
    op.create_index("ix_credential_secrets_created_at", TABLE, ["created_at"])

    if _is_postgres():
        op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {TABLE}_select ON {TABLE} FOR SELECT"
            f" USING (org_id = app.current_org())"
        )
        op.execute(
            f"CREATE POLICY {TABLE}_insert ON {TABLE} FOR INSERT"
            f" WITH CHECK (org_id = app.current_org())"
        )
        op.execute(
            f"CREATE POLICY {TABLE}_update ON {TABLE} FOR UPDATE"
            f" USING (org_id = app.current_org())"
            f" WITH CHECK (org_id = app.current_org())"
        )
        op.execute(
            f"CREATE POLICY {TABLE}_delete ON {TABLE} FOR DELETE"
            f" USING (org_id = app.current_org())"
        )
        _grant_runtime()


def _grant_runtime() -> None:
    """SELECT/INSERT/UPDATE for the runtime role. DELETE withheld.

    Revocation destroys the ciphertext rather than the row, so the runtime role never needs DELETE - and
    a role that cannot delete cannot make the record of a connection disappear.
    """
    op.execute("GRANT USAGE ON SCHEMA public TO granada_app")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {TABLE} TO granada_app")
    op.execute(f"REVOKE DELETE ON TABLE {TABLE} FROM granada_app")

    # Conditional on the role existing, matching 013: a hard GRANT to a role some deployments do not
    # have aborts the whole migration, and a schema change must not depend on an operational role.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_replica') THEN
                GRANT USAGE ON SCHEMA public TO granada_replica;
                GRANT SELECT ON TABLE {TABLE} TO granada_replica;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_table(TABLE)
