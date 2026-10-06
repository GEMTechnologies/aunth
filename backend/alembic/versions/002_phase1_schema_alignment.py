"""Phase 1: bring the migrated schema in line with the models

Revision ID: 002_phase1_schema_alignment
Revises: 001_initial_auth_schema
Create Date: 2026-10-06

Why
---
Revision 001 had never been executed: alembic.ini was a placeholder file and
alembic/env.py did not exist, so every database was built at start-up by
``Base.metadata.create_all()``. Once Alembic was made runnable, applying 001
produced a schema that did not match the models. ``tools/check_schema_drift.py``
reported exactly ten differences, all listed below.

This revision closes them. Nothing here drops or rewrites existing data; the
new columns are nullable or carry a default, so every statement is safe to run
against a populated database.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "002_phase1_schema_alignment"
down_revision = "001_initial_auth_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Columns present in the models but absent from revision 001.
    # All are nullable, so no backfill is required to apply this.
    # ------------------------------------------------------------------
    op.add_column("users", sa.Column("last_active_context", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("registration_intent", sa.String(length=50), nullable=True))

    # organisations.created_by is NOT NULL in the models. It is added as
    # nullable first so the migration runs against a populated table, then
    # backfilled and tightened.
    op.add_column("organisations", sa.Column("created_by", sa.String(length=36), nullable=True))
    op.execute("UPDATE organisations SET created_by = '' WHERE created_by IS NULL")
    with op.batch_alter_table("organisations") as batch_op:
        batch_op.alter_column(
            "created_by",
            existing_type=sa.String(length=36),
            nullable=False,
            server_default=sa.text("''"),
        )
    # Drop the server default again so the schema matches the model, which
    # applies the default in Python rather than in the database.
    with op.batch_alter_table("organisations") as batch_op:
        batch_op.alter_column(
            "created_by",
            existing_type=sa.String(length=36),
            nullable=False,
            server_default=None,
        )

    op.add_column("audit_logs", sa.Column("org_id", sa.String(length=36), nullable=True))
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.create_foreign_key(
            "fk_audit_logs_org_id",
            "organisations",
            ["org_id"],
            ["id"],
        )

    # ------------------------------------------------------------------
    # Tables present in the models but never created by a migration.
    # ------------------------------------------------------------------
    op.create_table(
        "oauth_accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_user_id", sa.String(), nullable=False),
        sa.Column("access_token", sa.String(), nullable=True),
        sa.Column("refresh_token", sa.String(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_oauth_accounts_user_id", "oauth_accounts", ["user_id"])
    op.create_index(
        "ix_oauth_accounts_provider_user_id",
        "oauth_accounts",
        ["provider", "provider_user_id"],
        unique=True,
    )

    op.create_table(
        "oauth_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_oauth_states_state", "oauth_states", ["state"], unique=True)

    # Created in Phase 1. OAuth callbacks used to return the access and
    # refresh tokens in the redirect query string; they now return a
    # single-use, short-lived, hashed code that is exchanged server-side.
    op.create_table(
        "oauth_auth_codes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("redirect_to", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_oauth_auth_codes_code_hash", "oauth_auth_codes", ["code_hash"], unique=True)
    op.create_index("ix_oauth_auth_codes_user_id", "oauth_auth_codes", ["user_id"])
    op.create_index("ix_oauth_auth_codes_expires_at", "oauth_auth_codes", ["expires_at"])

    op.create_table(
        "saml_providers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("sso_url", sa.String(), nullable=False),
        sa.Column("x509_cert", sa.String(), nullable=False),
        sa.Column("attribute_mapping", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organisations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "saml_assertions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("assertion_id", sa.String(), nullable=False),
        sa.Column("name_id", sa.String(), nullable=False),
        sa.Column("session_index", sa.String(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["saml_providers.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "user_contexts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("context_type", sa.String(length=50), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=True),
        sa.Column("product", sa.String(length=50), nullable=True),
        sa.Column("role", sa.String(length=100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organisations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_contexts_user_id", "user_contexts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_contexts_user_id", table_name="user_contexts")
    op.drop_table("user_contexts")
    op.drop_table("saml_assertions")
    op.drop_table("saml_providers")
    op.drop_index("ix_oauth_auth_codes_expires_at", table_name="oauth_auth_codes")
    op.drop_index("ix_oauth_auth_codes_user_id", table_name="oauth_auth_codes")
    op.drop_index("ix_oauth_auth_codes_code_hash", table_name="oauth_auth_codes")
    op.drop_table("oauth_auth_codes")
    op.drop_index("ix_oauth_states_state", table_name="oauth_states")
    op.drop_table("oauth_states")
    op.drop_index("ix_oauth_accounts_provider_user_id", table_name="oauth_accounts")
    op.drop_index("ix_oauth_accounts_user_id", table_name="oauth_accounts")
    op.drop_table("oauth_accounts")

    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.drop_constraint("fk_audit_logs_org_id", type_="foreignkey")
    op.drop_column("audit_logs", "org_id")
    op.drop_column("organisations", "created_by")
    op.drop_column("users", "registration_intent")
    op.drop_column("users", "last_active_context")