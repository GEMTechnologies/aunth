"""application workspace: the lifecycle state machine and its history.

Revision ID: 010_application_workspace
Revises: 009_decision_records
Create Date: 2026-10-07

Purpose
-------
Phase 6. One workspace per organisation and opportunity, with an append-only
transition history.

``applications``
    The current state. ``UniqueConstraint(org_id, opportunity_id)`` is what makes
    "one workspace per opportunity" true: two workspaces would mean two answers
    being written to the same funder.

``application_transitions``
    Every accepted transition, never updated and never deleted. This is what makes
    the brief's "full version history" real, and it is the difference between
    knowing an application was submitted and being able to say which version of
    it, by whom, and on whose authority.

Why the state set is not a CHECK constraint
-------------------------------------------
The transition table lives in ``agent/workspace.py`` rather than in a database
constraint, because a refused transition has to report *why*. A constraint
violation says a value is wrong; it cannot say "READY_TO_SUBMIT requires a receipt
from the funder". The guards that matter - readiness, approval, receipt - are the
ones a human needs explained.

Row-level security
------------------
ENABLE and FORCE on both tables. Which opportunities an organisation is pursuing,
and what it has written to which funder, is the most commercially sensitive data
the platform holds.

The transition table carries its own ``org_id`` rather than inheriting it through
a join, so its policy is a direct predicate rather than a subquery. That is a
deliberate departure from ``job_attempts``: an append-only audit trail is read on
its own far more often than it is read through its parent, and a subquery policy
would make the most audit-critical reads the slowest.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "010_application_workspace"
down_revision = "009_decision_records"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(table: str, name: str, command: str, using: str, check: str | None = None) -> None:
    """Mirrors the helper in 003-009; see 003 for the clause/command matrix."""
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
        "applications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False, index=True),
        sa.Column("opportunity_id", sa.String(36), sa.ForeignKey("opportunities.id"), nullable=False, index=True),
        sa.Column("state", sa.String(40), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("assigned_to", sa.String(36), nullable=True, index=True),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("submission_receipt", sa.Text(), nullable=True),
        sa.Column("submission_adapter", sa.String(50), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(40), nullable=True, index=True),
        sa.Column("correlation_id", sa.String(64), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("org_id", "opportunity_id", name="uq_application_org_opportunity"),
    )
    op.create_index(
        "ix_applications_org_state", "applications", ["org_id", "state", "deadline"]
    )
    op.create_index(
        "ix_applications_org_updated", "applications", ["org_id", "updated_at"]
    )

    op.create_table(
        "application_transitions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.id"), nullable=False, index=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False, index=True),
        sa.Column("from_state", sa.String(40), nullable=True),
        sa.Column("to_state", sa.String(40), nullable=False, index=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default="SYSTEM", index=True),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("decision_id", sa.String(36), nullable=True, index=True),
        sa.Column("job_id", sa.String(36), nullable=True, index=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("correlation_id", sa.String(64), nullable=True, index=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_index(
        "ix_transitions_application_version",
        "application_transitions", ["application_id", "version"],
    )

    if not _is_postgres():
        return

    for table in ("applications", "application_transitions"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        tenant = "org_id = app.current_org()"
        _policy(table, f"{table}_select", "SELECT", tenant)
        _policy(table, f"{table}_insert", "INSERT", "false", check=tenant)
        # No UPDATE or DELETE policy on the history table, and that is the
        # point: an append-only trail that its subject can rewrite is not one.
        # ``applications`` gets UPDATE because a workspace moves; the history
        # beside it cannot be edited.
        if table == "applications":
            _policy(table, f"{table}_update", "UPDATE", tenant, check=tenant)
            _policy(table, f"{table}_delete", "DELETE", tenant)

    _grant_runtime()


def _grant_runtime() -> None:
    """SELECT/INSERT on the history, full DML on the workspace.

    ``application_transitions`` gets **no UPDATE and no DELETE**, at the grant
    layer as well as in policy. That is double protection on purpose: the audit
    trail is the one table where a mistake has no recovery, and the runtime role
    has no business editing history it wrote.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON applications TO granada_app;
                GRANT SELECT, INSERT ON application_transitions TO granada_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_transitions_application_version", table_name="application_transitions")
    op.drop_table("application_transitions")
    op.drop_index("ix_applications_org_updated", table_name="applications")
    op.drop_index("ix_applications_org_state", table_name="applications")
    op.drop_table("applications")
