"""Award-to-delivery: grants, projects, conditions, reporting obligations, disbursements.

Revision ID: 017_delivery
Revises: 016_submission

Phase 9. Every table here is **derived** from the authorised submission package rather
than typed in, because the brief's exit criterion for this phase is that no data already
approved in the application is re-entered by hand. Re-keying an approved budget is how
the record and the application drift apart, and the drift is only discovered at audit.

**DELETE is revoked on all five.** A grant that was created in error is not deleted; it
is terminated, suspended or cancelled, which leaves a record that it existed and why it
ended. Money that arrived and then vanished from the system is worse than money that
never arrived, because nobody knows to look for it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "017_delivery"
down_revision = "016_submission"
branch_labels = None
depends_on = None

TABLES = (
    "grants",
    "projects",
    "grant_conditions",
    "reporting_obligations",
    "disbursements",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _tenant_columns() -> list[sa.Column]:
    return [
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
    ]


def _index(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    op.create_table(
        "grants",
        sa.Column("id", sa.String(36), primary_key=True),
        *_tenant_columns(),
        sa.Column("application_id", sa.String(36), nullable=False),
        sa.Column("opportunity_id", sa.String(36)),
        # Provenance, not convenience: every figure below is traceable to what a person
        # authorised in Phase 8.
        sa.Column("source_package_id", sa.String(36)),
        sa.Column("reference", sa.String(255), nullable=False),
        sa.Column("donor_name", sa.String(255)),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("requested_amount", sa.Numeric(18, 2)),
        sa.Column("awarded_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("size_relative_to_request", sa.String(20), nullable=False,
                  server_default="AS_REQUESTED"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("awarded_at", sa.DateTime(timezone=True)),
        sa.Column("starts_on", sa.DateTime(timezone=True)),
        sa.Column("ends_on", sa.DateTime(timezone=True)),
        sa.Column("mail_thread_id", sa.String(36)),
        sa.Column("donor_contact_email", sa.String(320)),
        sa.Column("approved_budget", sa.JSON),
        sa.Column("awarded_budget", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(64)),
        sa.UniqueConstraint("org_id", "reference", name="uq_grant_org_reference"),
    )
    _index("grants", "org_id", "agent_id", "application_id", "opportunity_id",
           "source_package_id", "reference", "status", "ends_on", "created_at",
           "mail_thread_id", "correlation_id")

    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        *_tenant_columns(),
        sa.Column("grant_id", sa.String(36), sa.ForeignKey("grants.id"), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PLANNED"),
        sa.Column("baseline_workplan", sa.JSON),
        sa.Column("budget_total", sa.Numeric(18, 2)),
        sa.Column("starts_on", sa.DateTime(timezone=True)),
        sa.Column("ends_on", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _index("projects", "org_id", "agent_id", "grant_id", "status", "created_at")

    op.create_table(
        "grant_conditions",
        sa.Column("id", sa.String(36), primary_key=True),
        *_tenant_columns(),
        sa.Column("grant_id", sa.String(36), sa.ForeignKey("grants.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="OTHER"),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("blocks_payment", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("due_on", sa.DateTime(timezone=True)),
        sa.Column("satisfied_at", sa.DateTime(timezone=True)),
        sa.Column("satisfied_by", sa.String(36)),
        sa.Column("evidence_ref", sa.String(500)),
        sa.Column("evidence_note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _index("grant_conditions", "org_id", "agent_id", "grant_id", "kind", "status",
           "blocks_payment", "due_on", "created_at")

    op.create_table(
        "reporting_obligations",
        sa.Column("id", sa.String(36), primary_key=True),
        *_tenant_columns(),
        sa.Column("grant_id", sa.String(36), sa.ForeignKey("grants.id"), nullable=False),
        sa.Column("project_id", sa.String(36)),
        sa.Column("kind", sa.String(20), nullable=False, server_default="NARRATIVE"),
        sa.Column("period", sa.String(20), nullable=False, server_default="ONE_OFF"),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("due_on", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_starts_on", sa.DateTime(timezone=True)),
        sa.Column("period_ends_on", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_by", sa.String(36)),
        # Required for SUBMITTED, enforced by the service: believing a report was filed
        # when it was not is worse than knowing it is late.
        sa.Column("reference", sa.String(255)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("report_document_ref", sa.String(500)),
        sa.Column("remind_days_before", sa.Integer, nullable=False, server_default="14"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _index("reporting_obligations", "org_id", "agent_id", "grant_id", "project_id",
           "kind", "period", "status", "due_on", "reference", "created_at")

    op.create_table(
        "disbursements",
        sa.Column("id", sa.String(36), primary_key=True),
        *_tenant_columns(),
        sa.Column("grant_id", sa.String(36), sa.ForeignKey("grants.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="EXPECTED"),
        sa.Column("label", sa.String(255)),
        sa.Column("tranche_number", sa.Integer),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("expected_on", sa.DateTime(timezone=True)),
        sa.Column("received_on", sa.DateTime(timezone=True)),
        sa.Column("reference", sa.String(255)),
        sa.Column("gated_by_condition_ids", sa.JSON),
        sa.Column("amount_received", sa.Numeric(18, 2)),
        sa.Column("variance_note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _index("disbursements", "org_id", "agent_id", "grant_id", "status", "expected_on",
           "received_on", "reference", "created_at")

    if not _is_postgres():
        return

    for table in TABLES:
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
    """UPDATE allowed, DELETE revoked, on every delivery table.

    The additive ``ALTER DEFAULT PRIVILEGES`` block in ``sql/grant_runtime_role.sql`` has
    re-granted privileges on an append-only table eight times in this project, so the
    REVOKE lives in the migration as well as in the script. Both places, deliberately:
    the migration establishes it, the script must not undo it.
    """
    op.execute("GRANT USAGE ON SCHEMA public TO granada_app")
    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO granada_app")
        # A grant created in error is terminated, not deleted. Deleting it destroys the
        # record that money was expected and why it stopped, which is precisely what an
        # auditor asks for.
        op.execute(f"REVOKE DELETE ON TABLE {table} FROM granada_app")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_replica') THEN
                GRANT USAGE ON SCHEMA public TO granada_replica;
                GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO granada_replica;
                REVOKE DELETE ON TABLE grants FROM granada_replica;
                REVOKE DELETE ON TABLE projects FROM granada_replica;
                REVOKE DELETE ON TABLE grant_conditions FROM granada_replica;
                REVOKE DELETE ON TABLE reporting_obligations FROM granada_replica;
                REVOKE DELETE ON TABLE disbursements FROM granada_replica;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
