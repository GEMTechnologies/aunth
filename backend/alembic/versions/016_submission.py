"""Submission packages, attempts and receipts (Phase 8).

Revision ID: 016_submission
Revises: 015_autonomous_policy

Three tables, following the Phase 7b shape deliberately rather than inventing a second
one:

``submission_packages``
    The frozen artefact set. ``package_fingerprint`` is what a human authorises, so
    changing a document, an answer or a figure invalidates it. Mutable, because its
    status advances.
``submission_attempts``
    **Append-only.** The three outcomes, because a timeout during submission does not
    mean the funder did not receive it - and filing a second application is worse than
    a duplicate email, because many programmes disqualify both bids.
``submission_receipts``
    Evidence that a funder received it. The workspace already refuses ``SUBMITTED``
    without an external reference, and this is where that reference lives.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016_submission"
down_revision = "015_autonomous_policy"
branch_labels = None
depends_on = None

AGENT_SCOPED = ("submission_packages", "submission_attempts", "submission_receipts")

#: History that must never be rewritten.
APPEND_ONLY = ("submission_attempts",)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "submission_packages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("application_id", sa.String(36), nullable=False),
        sa.Column("opportunity_id", sa.String(36)),
        sa.Column("package_fingerprint", sa.String(64), nullable=False),
        sa.Column("fingerprint_input", sa.Text),
        sa.Column("manifest", sa.JSON),
        sa.Column("application_version", sa.Integer),
        sa.Column("status", sa.String(30), nullable=False, server_default="DRAFT"),
        sa.Column("status_reason", sa.Text),
        sa.Column("submission_mode", sa.String(20), nullable=False, server_default="HANDOFF"),
        sa.Column("agent_version", sa.Integer),
        sa.Column("target_url", sa.String(1000)),
        sa.Column("provider", sa.String(40)),
        sa.Column("provider_submission_id", sa.String(255)),
        sa.Column("funder_reference", sa.String(255)),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorised_at", sa.DateTime(timezone=True)),
        sa.Column("authorised_by", sa.String(36)),
        sa.Column("handoff_ready_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("retry_not_before", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(60)),
        sa.Column("failure_summary", sa.Text),
        sa.Column("correlation_id", sa.String(64)),
        # One authorised package is one application. The constraint is the guarantee.
        sa.UniqueConstraint("idempotency_key", name="uq_submission_package_idempotency"),
    )
    for column in (
        "org_id", "agent_id", "application_id", "opportunity_id", "package_fingerprint",
        "status", "submission_mode", "provider", "provider_submission_id",
        "funder_reference", "created_at", "submitted_at", "failure_code", "correlation_id",
    ):
        op.create_index(f"ix_submission_packages_{column}", "submission_packages", [column])

    op.create_table(
        "submission_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("package_id", sa.String(36), sa.ForeignKey("submission_packages.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("request_fingerprint", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("result", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(60)),
        sa.Column("safe_error_summary", sa.Text),
        sa.Column("provider_submission_id", sa.String(255)),
        sa.Column("reconciliation_state", sa.String(20), nullable=False, server_default="UNKNOWN"),
        sa.Column("reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in (
        "org_id", "agent_id", "package_id", "attempt_id", "provider", "result",
        "error_code", "reconciliation_state", "started_at", "created_at",
    ):
        op.create_index(f"ix_submission_attempts_{column}", "submission_attempts", [column])

    op.create_table(
        "submission_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("package_id", sa.String(36), sa.ForeignKey("submission_packages.id"), nullable=False),
        sa.Column("application_id", sa.String(36), nullable=False),
        sa.Column("reference", sa.String(255), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("acknowledgement_text", sa.Text),
        sa.Column("evidence_ref", sa.String(500)),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_by", sa.String(36)),
        sa.Column("recorded_by_agent", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    for column in ("org_id", "agent_id", "package_id", "application_id", "reference", "source", "captured_at"):
        op.create_index(f"ix_submission_receipts_{column}", "submission_receipts", [column])

    if not _is_postgres():
        return

    for table in AGENT_SCOPED:
        op.execute(
            f"""
            ALTER TABLE {table}
              ADD CONSTRAINT fk_{table}_agent_org
              FOREIGN KEY (agent_id, org_id)
              REFERENCES granada_agents (id, org_id)
            """
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_select ON {table} FOR SELECT"
            f" USING (org_id = app.current_org())"
        )
        op.execute(
            f"CREATE POLICY {table}_insert ON {table} FOR INSERT"
            f" WITH CHECK (org_id = app.current_org())"
        )
        op.execute(
            f"CREATE POLICY {table}_update ON {table} FOR UPDATE"
            f" USING (org_id = app.current_org())"
            f" WITH CHECK (org_id = app.current_org())"
        )
        op.execute(
            f"CREATE POLICY {table}_delete ON {table} FOR DELETE"
            f" USING (org_id = app.current_org())"
        )

    _grant_runtime()


def _grant_runtime() -> None:
    """Grants, with the append-only posture established here as well as in the script.

    The additive ``ALTER DEFAULT PRIVILEGES`` trap has re-granted UPDATE or DELETE on an
    append-only table **eight times** in this project. Putting the REVOKE in the
    migration means the posture is established by the schema change itself, and
    ``sql/grant_runtime_role.sql`` carries the same block so re-running it does not undo
    this.
    """
    tables = ("submission_packages", "submission_attempts", "submission_receipts")
    op.execute("GRANT USAGE ON SCHEMA public TO granada_app")
    for table in tables:
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO granada_app")
        op.execute(f"REVOKE DELETE ON TABLE {table} FROM granada_app")

    # An attempt is history: a row saying "we handed this to the funder and never
    # learned what happened" is the only evidence reconciliation has. A receipt is
    # evidence too. Neither may be rewritten.
    for table in ("submission_attempts", "submission_receipts"):
        op.execute(f"REVOKE UPDATE ON TABLE {table} FROM granada_app")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_replica') THEN
                GRANT USAGE ON SCHEMA public TO granada_replica;
                GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO granada_replica;
                REVOKE UPDATE ON TABLE submission_attempts FROM granada_replica;
                REVOKE UPDATE ON TABLE submission_receipts FROM granada_replica;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    for table in ("submission_receipts", "submission_attempts", "submission_packages"):
        op.drop_table(table)
