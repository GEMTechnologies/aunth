"""decision records: the durable audit trail for bounded decisions.

Revision ID: 009_decision_records
Revises: 008_opportunity_matching
Create Date: 2026-10-07

Purpose
-------
Phase 5b. The Decision Gateway records every decision it makes, so an automated
action can be explained afterwards with named fields rather than the sentence
"AI decided yes".

The two columns that carry the design:

``state_hash``
    A fingerprint of the state the decision was made on. It makes the cache key
    correct *and* makes a stored decision invalidatable - when the organisation
    profile or the opportunity changes, the fingerprint changes and the old
    verdict stops being reusable. Without it, a cached answer outlives the facts
    it was based on.

``shadow`` / ``shadow_of``
    Shadow decisions are recorded with a pointer to the decision that actually
    acted. That is what lets the platform measure how often Jev would have agreed
    with the rules, using real Granada data, while being *structurally* unable to
    let the shadow answer influence anything.

``state_snapshot`` is nullable and off by default: the state may contain donor
and beneficiary content, and a hash plus references is enough to audit with.

Row-level security
------------------
ENABLE and FORCE. A decision record names what an organisation was judged
eligible for, what its mail said and what it was authorised to do. It is as
sensitive as the profile it was derived from.

System-level decisions - classifying an inbound webhook that has not been
correlated to a tenant yet - legitimately have a NULL organisation, and under
FORCE an unscoped row is invisible to every tenant, which is the correct default
for unattributed work.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "009_decision_records"
down_revision = "008_opportunity_matching"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(table: str, name: str, command: str, using: str, check: str | None = None) -> None:
    """Mirrors the helper in 003-008; see 003 for the clause/command matrix."""
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
        "decision_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=True, index=True),
        sa.Column("organisation_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=True, index=True),
        sa.Column("decision_type", sa.String(80), nullable=False, index=True),
        sa.Column("subject_type", sa.String(20), nullable=True, index=True),
        sa.Column("subject_id", sa.String(36), nullable=True, index=True),
        sa.Column("workflow_id", sa.String(36), nullable=True, index=True),
        sa.Column("provider", sa.String(50), nullable=False, index=True),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("question_schema_version", sa.String(20), nullable=False, server_default="v1", index=True),
        sa.Column("state_hash", sa.String(64), nullable=False, index=True),
        sa.Column("state_snapshot", sa.JSON(), nullable=True),
        sa.Column("answers", sa.JSON(), nullable=True),
        sa.Column("confidences", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True, index=True),
        sa.Column("probabilities", sa.JSON(), nullable=True),
        sa.Column("policy_outcome", sa.Boolean(), nullable=True, index=True),
        sa.Column("policy_detail", sa.JSON(), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.false(), index=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shadow", sa.Boolean(), nullable=False, server_default=sa.false(), index=True),
        sa.Column("shadow_of", sa.String(36), nullable=True, index=True),
        sa.Column("correlation_id", sa.String(64), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True, index=True),
    )
    op.create_index(
        "ix_decisions_org_type_created",
        "decision_records", ["organisation_id", "decision_type", "created_at"],
    )
    op.create_index(
        "ix_decisions_cache_lookup",
        "decision_records",
        ["organisation_id", "decision_type", "state_hash", "provider"],
    )

    if not _is_postgres():
        return

    op.execute('ALTER TABLE "decision_records" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "decision_records" FORCE ROW LEVEL SECURITY')

    # The tenant-or-system predicate, matching outbox_events: a decision with no
    # organisation is pre-tenant infrastructure, and only an unscoped context can
    # create or read those.
    tenant_or_system = "organisation_id IS NULL OR organisation_id = app.current_org()"
    system_only = "organisation_id = app.current_org()"
    _policy("decision_records", "decisions_select", "SELECT", tenant_or_system)
    _policy("decision_records", "decisions_insert", "INSERT", "false", check=tenant_or_system)
    _policy("decision_records", "decisions_update", "UPDATE", system_only, check=system_only)
    # Never deletable: an audit trail that the audited party can erase is not one.
    _policy("decision_records", "decisions_delete", "DELETE", "false")

    _grant_runtime()


def _grant_runtime() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT, INSERT, UPDATE ON decision_records TO granada_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_decisions_cache_lookup", table_name="decision_records")
    op.drop_index("ix_decisions_org_type_created", table_name="decision_records")
    op.drop_table("decision_records")
