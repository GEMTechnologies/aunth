"""model invocations: the record of every model call.

Revision ID: 005_model_invocations
Revises: 004_agent_runtime_ledger
Create Date: 2026-10-07

Purpose
-------
Phase 2c. Three obligations meet in one table:

**Cost control.** An autonomous platform that can spend money without
recording what it spent cannot be trusted with autonomy. Cost is an integer
count of micro-dollars; money in binary floating point accumulates error, and
a budget guard that drifts is worse than none.

**The "why?" trail.** Every automated decision must be explainable afterwards,
so provider, model, model version, tier and prompt version are first-class
columns rather than fields inside a JSON blob.

**Data minimisation.** The prompt and response are stored as SHA-256 digests.
The audit question is "was this the same input, and what did it produce", which
a digest answers; retaining donor and beneficiary text in an ops table answers
a question nobody should be asking. ``MODEL_STORE_PROMPTS`` overrides that, and
turning it on is a deliberate decision with a privacy cost.

Row-level security
------------------
ENABLE and FORCE, matching ``jobs`` in 004. These rows name a tenant's
opportunities and decisions, and nothing legitimately needs to read another
tenant's spend.

``org_id`` is nullable because system-level work - classifying an inbound
webhook that has not been correlated to a tenant yet - legitimately has no
tenant. Under FORCE, an unscoped row is invisible to every tenant, and that is
the correct default for unattributed work.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "005_model_invocations"
down_revision = "004_agent_runtime_ledger"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(table: str, name: str, command: str, using: str, check: str | None = None) -> None:
    """Create one policy, emitting only the clauses PostgreSQL accepts.

    Mirrors the helper in 003 and 004 on purpose: the clause/command matrix is
    not symmetric and the server rejects the wrong pairing outright, so it is
    enforced in one place rather than left to each caller.

    * ``USING`` is rejected on INSERT.
    * ``WITH CHECK`` is rejected on SELECT and DELETE.
    * UPDATE accepts both.
    """
    command = command.upper()
    parts = [f'CREATE POLICY "{name}" ON "{table}" FOR {command} TO PUBLIC']
    if command == "INSERT":
        parts.append(f"WITH CHECK ({check if check is not None else using})")
    elif command == "UPDATE":
        parts.append(f"USING ({using})")
        parts.append(f"WITH CHECK ({check if check is not None else using})")
    else:  # SELECT, DELETE
        parts.append(f"USING ({using})")

    op.execute(f'DROP POLICY IF EXISTS "{name}" ON "{table}"')
    op.execute(" ".join(parts))


def upgrade() -> None:
    op.create_table(
        "model_invocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=True, index=True),
        sa.Column("job_id", sa.String(36), nullable=True, index=True),
        sa.Column("provider", sa.String(50), nullable=False, index=True),
        sa.Column("model", sa.String(120), nullable=False, index=True),
        sa.Column("model_version", sa.String(120), nullable=True),
        sa.Column("tier", sa.String(20), nullable=False, index=True),
        sa.Column("prompt_version", sa.String(50), nullable=False),
        sa.Column("prompt_digest", sa.String(64), nullable=False),
        sa.Column("response_digest", sa.String(64), nullable=True),
        sa.Column("prompt_text", sa.Text(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_micros", sa.Integer(), nullable=False, server_default="0", index=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="SUCCEEDED", index=True),
        sa.Column("error_category", sa.String(40), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_index(
        "ix_model_invocations_org_created", "model_invocations", ["org_id", "created_at"]
    )
    op.create_index(
        "ix_model_invocations_model_status", "model_invocations", ["model", "status"]
    )

    if not _is_postgres():
        # Documented no-op, matching 003 and 004. SQLite has no row-level
        # security, so on SQLite the tenant boundary is the application layer
        # alone - which is exactly why the request-path probe runs against real
        # PostgreSQL.
        return

    op.execute('ALTER TABLE "model_invocations" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "model_invocations" FORCE ROW LEVEL SECURITY')

    tenant = "org_id = app.current_org()"
    _policy("model_invocations", "model_invocations_select", "SELECT", tenant)
    _policy("model_invocations", "model_invocations_insert", "INSERT", "false", check=tenant)
    _policy("model_invocations", "model_invocations_update", "UPDATE", tenant, check=tenant)
    _policy("model_invocations", "model_invocations_delete", "DELETE", tenant)

    _grant_runtime()


def _grant_runtime() -> None:
    """Grant the runtime role least-privilege DML on the new table.

    SELECT, INSERT and UPDATE only - no DELETE. An invocation record is the
    evidence for what the platform spent and decided; the application role has
    no business erasing it. Schema access and ``alembic_version`` stay
    withheld, matching ``sql/grant_runtime_role.sql``.

    Note that this narrows what the blanket grant in
    ``sql/grant_runtime_role.sql`` would otherwise confer, so that file must be
    re-run with care: it grants DELETE on all tables.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT, INSERT, UPDATE ON model_invocations TO granada_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_model_invocations_model_status", table_name="model_invocations")
    op.drop_index("ix_model_invocations_org_created", table_name="model_invocations")
    op.drop_table("model_invocations")
