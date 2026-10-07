"""The autonomous policy evidence on an approval (Phase 7c).

Revision ID: 015_autonomous_policy
Revises: 014_outbound_mail

One column. ``mail_approvals.policy_evidence`` records every gate and its result for
an ``AUTONOMOUS_POLICY`` decision, so "why did the agent send this without asking?"
is answerable from the record rather than from whichever code happened to be deployed
at the time.

The model gained the column first and the migration did not, which
``test_schema_drift`` catches - the models and the migrations must describe the same
schema or `create_all` in tests silently exercises something the deployed database
does not have.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "015_autonomous_policy"
down_revision = "014_outbound_mail"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column("mail_approvals", sa.Column("policy_evidence", sa.JSON))

    if not _is_postgres():
        return

    # The runtime role already holds SELECT/INSERT on this table and no UPDATE. An
    # added column inherits the table's grants, but the additive
    # ALTER DEFAULT PRIVILEGES trap has re-granted UPDATE on an append-only table
    # seven times in this project, so the posture is re-asserted here explicitly
    # rather than assumed to have carried over.
    op.execute("REVOKE UPDATE ON TABLE mail_approvals FROM granada_app")
    op.execute("REVOKE DELETE ON TABLE mail_approvals FROM granada_app")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_replica') THEN
                REVOKE UPDATE ON TABLE mail_approvals FROM granada_replica;
                REVOKE DELETE ON TABLE mail_approvals FROM granada_replica;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_column("mail_approvals", "policy_evidence")
