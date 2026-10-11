"""Verification requests: the anchor that keeps a code from completing the wrong registration.

Revision ID: 021_mail_verification
Revises: 020_credential_deny_fleet
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "021_mail_verification"
down_revision: Union[str, None] = "020_credential_deny_fleet"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "mail_verification_requests"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=True),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("portal_host", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(120), nullable=False),
        sa.Column("browser_task_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("satisfied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("satisfied_message_id", sa.String(36), nullable=True),
        sa.Column("result_kind", sa.String(12), nullable=True),
        sa.Column("result_code", sa.String(32), nullable=True),
        sa.Column("result_link", sa.String(2048), nullable=True),
        sa.Column("fingerprint", sa.String(200), nullable=True),
        sa.Column("examined", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # THE UNIQUENESS THAT MAKES APPLYING ONE CODE TWICE A NO-OP, declared INLINE.
        #
        # It was added afterwards with op.create_unique_constraint, which SQLite cannot do - SQLite
        # has no ALTER TABLE ADD CONSTRAINT, so alembic raised NotImplementedError and the schema-drift
        # test died at this revision. Declaring it with the table produces the same schema on both
        # engines, which is what the drift test is for.
        sa.UniqueConstraint("org_id", "fingerprint", name="uq_mail_verification_fingerprint"),
    )
    op.create_index("ix_mail_verification_requests_org_id", TABLE, ["org_id"])
    op.create_index("ix_mail_verification_requests_status", TABLE, ["status"])
    op.create_index("ix_mail_verification_requests_expires_at", TABLE, ["expires_at"])
    op.create_index("ix_mail_verification_pending", TABLE, ["org_id", "status"])

    if not _is_postgres():
        return

    # TENANT ISOLATION, the same shape as every other organisation-scoped table: the row is visible
    # only under the organisation's own binding.
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE tablename = '{TABLE}' AND policyname = '{TABLE}_org_isolation'
            ) THEN
                CREATE POLICY {TABLE}_org_isolation ON {TABLE}
                    USING (org_id = app.current_org())
                    WITH CHECK (org_id = app.current_org());
            END IF;
        END
        $$;
        """
    )

    # Grants are applied explicitly rather than relying on a later script, so a fresh database and an
    # upgraded one end up identical.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT, INSERT, UPDATE ON TABLE {TABLE} TO granada_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_table(TABLE)
