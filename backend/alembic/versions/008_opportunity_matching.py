"""matching: gate every opportunity against the organisation, then rank.

Revision ID: 008_opportunity_matching
Revises: 007_opportunity_catalogue
Create Date: 2026-10-07

Purpose
-------
Phase 5. One tenant-owned table recording, per organisation and opportunity,
whether a deterministic eligibility gate rejected it and - if not - how it ranked.

The security-relevant property of this table is that ``semantic_score`` is
NULLABLE and is only populated for a match that passed every hard gate. That is
not a convention: the ranking engine structurally never computes a score for an
ineligible opportunity, so there is no number available for anything downstream
to be tempted by. A high semantic score cannot override a hard eligibility
failure if no such score exists.

``REJECTED_BY_RULE`` rows are retained. An organisation that cannot see what it
was ruled out of, and on what basis, cannot correct its own profile.

Row-level security
------------------
ENABLE and FORCE, like every other tenant-owned table. Who is pursuing which
funding is among the most commercially sensitive data in the product.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "008_opportunity_matching"
down_revision = "007_opportunity_catalogue"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(table: str, name: str, command: str, using: str, check: str | None = None) -> None:
    """Mirrors the helper in 003-007; see 003 for the clause/command matrix."""
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
        "opportunity_matches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False, index=True),
        sa.Column("opportunity_id", sa.String(36), sa.ForeignKey("opportunities.id"), nullable=False, index=True),
        sa.Column("state", sa.String(20), nullable=False, index=True),
        sa.Column("hard_gate_passed", sa.Boolean(), nullable=False, server_default=sa.false(), index=True),
        sa.Column("failed_gates", sa.JSON(), nullable=True),
        sa.Column("unknown_gates", sa.JSON(), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=True),
        # Nullable by design: see the module docstring. Only a match that passed
        # every hard gate can carry a score.
        sa.Column("semantic_score", sa.Float(), nullable=True, index=True),
        sa.Column("final_score", sa.Float(), nullable=True, index=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("scorer", sa.String(120), nullable=True),
        sa.Column("prompt_version", sa.String(50), nullable=True),
        sa.Column("contract_version", sa.String(20), nullable=False, server_default="v1"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, index=True),
        # One current verdict per (tenant, opportunity). Recomputing supersedes
        # rather than accumulating duplicates, so a uniqueness violation is what
        # stops a race between two matching runs.
        sa.UniqueConstraint("org_id", "opportunity_id", name="uq_match_org_opportunity"),
    )
    op.create_index(
        "ix_matches_org_state_score",
        "opportunity_matches", ["org_id", "state", "final_score"],
    )
    op.create_index(
        "ix_matches_org_rank", "opportunity_matches", ["org_id", "rank"]
    )

    if not _is_postgres():
        return

    op.execute('ALTER TABLE "opportunity_matches" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "opportunity_matches" FORCE ROW LEVEL SECURITY')

    tenant = "org_id = app.current_org()"
    _policy("opportunity_matches", "matches_select", "SELECT", tenant)
    _policy("opportunity_matches", "matches_insert", "INSERT", "false", check=tenant)
    _policy("opportunity_matches", "matches_update", "UPDATE", tenant, check=tenant)
    # DELETE is permitted to the tenant: a match is derived data, recomputable
    # from the catalogue and the profile, unlike the ledger or the vault. Making
    # it undeletable would buy no integrity and would block re-matching.
    _policy("opportunity_matches", "matches_delete", "DELETE", tenant)

    _grant_runtime()


def _grant_runtime() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON opportunity_matches TO granada_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_matches_org_rank", table_name="opportunity_matches")
    op.drop_index("ix_matches_org_state_score", table_name="opportunity_matches")
    op.drop_table("opportunity_matches")
