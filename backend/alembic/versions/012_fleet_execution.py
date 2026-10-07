"""fleet execution: the agent/organisation invariant, provenance and activity.

Revision ID: 012_fleet_execution
Revises: 011_granada_agent
Create Date: 2026-10-07

Purpose
-------
Phase 6c makes the first Granada Agent actually work. Before any real work is
dispatched, this revision closes the hole that would make it dangerous.

The invariant
-------------
A durable row must never be able to say *organisation A* while naming *agent B's
agent*. The worker already derives the agent from the durable job row rather than
trusting the message, so the row itself is now the last line of defence - and it
was not defended at all. A service bug, a bad migration or a manual statement
could have produced a job that acted for one organisation while carrying another's
authority ceiling.

Enforced the way the brief asks, with a **composite foreign key**:

    (agent_id, org_id) REFERENCES granada_agents(id, org_id)

which requires a unique key on ``granada_agents(id, org_id)``. ``id`` alone is
already unique, so this adds no new uniqueness claim; it gives PostgreSQL the
index it needs to enforce the *pair*.

``jobs.org_id`` is nullable, and PostgreSQL's default MATCH SIMPLE semantics are
exactly right here: if **any** column of a composite key is NULL, the constraint is
not checked. So a system job with no agent is unconstrained, while a job that
names an agent must name the matching organisation. That is the intended shape and
it is why the column stays nullable rather than being forced non-null.

Where both IDs legitimately exist
---------------------------------
``jobs`` and ``agent_workflows`` carry both, so both get the composite key.

``agent_activity`` and ``donor_research`` are new here and carry both, so both get
it too - and they carry ``org_id`` for a second reason as well: their RLS policy
filters on it directly rather than reaching tenancy through a join.

Where duplication is deliberately NOT added
-------------------------------------------
``applications`` and ``decision_records`` are **not** given an ``agent_id``.
There is exactly one agent per organisation (``uq_agent_org``), so the agent is a
total function of ``org_id`` and a denormalised copy could only ever disagree with
it. Adding one would create a second source of truth for "whose agent is this" in
exchange for nothing - the join is one indexed lookup on a unique key.

The rule applied: duplicate the pair only where the pair is genuinely stored, and
where it is not, say so rather than adding a column that can drift.

Also added
----------
``jobs.agent_version`` — the authority version at creation time.
``jobs.workflow_id``  — which workflow the job advances.
``jobs.started_executing_at`` — distinguishes "leased" from "never started".
``opportunities.version`` — so research can record which revision it read.
``agent_activity`` — the customer-facing, structured activity ledger.
``donor_research`` — versioned research with per-field epistemic class.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "012_fleet_execution"
down_revision = "011_granada_agent"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(table: str, name: str, command: str, using: str, check: str | None = None) -> None:
    """Mirrors the helper in 003-011; see 003 for the clause/command matrix."""
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
    # -- new tables ---------------------------------------------------------
    op.create_table(
        "agent_activity",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False, index=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False, index=True),
        sa.Column("specialist_key", sa.String(40), nullable=True, index=True),
        sa.Column("workflow_id", sa.String(36), nullable=True, index=True),
        sa.Column("job_id", sa.String(36), nullable=True, index=True),
        sa.Column("activity_type", sa.String(60), nullable=False, index=True),
        sa.Column("summary_key", sa.String(80), nullable=False, index=True),
        sa.Column("subject_type", sa.String(20), nullable=True, index=True),
        sa.Column("subject_id", sa.String(36), nullable=True, index=True),
        sa.Column("structured_data", sa.JSON(), nullable=True),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="CUSTOMER", index=True),
        sa.Column("correlation_id", sa.String(64), nullable=True, index=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_index("ix_activity_agent_time", "agent_activity", ["agent_id", "occurred_at"])
    op.create_index(
        "ix_activity_customer", "agent_activity", ["org_id", "visibility", "occurred_at"]
    )

    op.create_table(
        "donor_research",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False, index=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False, index=True),
        sa.Column("opportunity_id", sa.String(36), sa.ForeignKey("opportunities.id"), nullable=False, index=True),
        sa.Column("application_id", sa.String(36), nullable=True, index=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("donor_identity", sa.JSON(), nullable=True),
        sa.Column("programme_priorities", sa.JSON(), nullable=True),
        sa.Column("eligibility_observations", sa.JSON(), nullable=True),
        sa.Column("application_instructions", sa.Text(), nullable=True),
        sa.Column("funding_range", sa.JSON(), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("required_documents", sa.JSON(), nullable=True),
        sa.Column("required_sections", sa.JSON(), nullable=True),
        sa.Column("submission_mechanism", sa.String(120), nullable=True),
        sa.Column("contacts", sa.JSON(), nullable=True),
        sa.Column("risks", sa.JSON(), nullable=True),
        sa.Column("unknowns", sa.JSON(), nullable=True),
        sa.Column("fact_classes", sa.JSON(), nullable=True),
        sa.Column("source_references", sa.JSON(), nullable=True),
        sa.Column("opportunity_version", sa.Integer(), nullable=True),
        sa.Column("research_version", sa.String(20), nullable=False, server_default="v1"),
        sa.Column("researched_at", sa.DateTime(timezone=True), nullable=False, index=True),
        # Never overwrite the result an existing application was built against.
        sa.UniqueConstraint(
            "opportunity_id", "agent_id", "version", name="uq_research_opportunity_version"
        ),
    )
    op.create_index(
        "ix_research_current", "donor_research", ["opportunity_id", "is_current"]
    )

    # -- columns on existing tables ----------------------------------------
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("agent_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("workflow_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("started_executing_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_jobs_workflow", ["workflow_id"])

    with op.batch_alter_table("opportunities") as batch:
        batch.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )

    # -- THE invariant ------------------------------------------------------
    # One agent per organisation, needed as a unique key before the composite
    # foreign keys below can reference the *pair*.
    op.create_index(
        "ix_agents_id_org", "granada_agents", ["id", "org_id"], unique=True
    )

    if _is_postgres():
        # Composite FK: a row naming an agent must name that agent's organisation.
        # MATCH SIMPLE (the default) skips the check when any column is NULL, which
        # is exactly what allows a null-agent system job while forbidding a
        # mismatched pair.
        op.execute(
            """
            ALTER TABLE jobs
              ADD CONSTRAINT fk_jobs_agent_org
              FOREIGN KEY (agent_id, org_id)
              REFERENCES granada_agents (id, org_id)
            """
        )
        op.execute(
            """
            ALTER TABLE agent_workflows
              ADD CONSTRAINT fk_workflow_agent_org
              FOREIGN KEY (agent_id, org_id)
              REFERENCES granada_agents (id, org_id)
            """
        )
        op.execute(
            """
            ALTER TABLE agent_activity
              ADD CONSTRAINT fk_activity_agent_org
              FOREIGN KEY (agent_id, org_id)
              REFERENCES granada_agents (id, org_id)
            """
        )
        op.execute(
            """
            ALTER TABLE donor_research
              ADD CONSTRAINT fk_research_agent_org
              FOREIGN KEY (agent_id, org_id)
              REFERENCES granada_agents (id, org_id)
            """
        )

        for table in ("agent_activity", "donor_research"):
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            tenant = "org_id = app.current_org()"
            _policy(table, f"{table}_select", "SELECT", tenant)
            _policy(table, f"{table}_insert", "INSERT", "false", check=tenant)
            _policy(table, f"{table}_update", "UPDATE", tenant, check=tenant)
            _policy(table, f"{table}_delete", "DELETE", tenant)

        # Inside the PostgreSQL guard: the DO block is PL/pgSQL and SQLite cannot
        # parse it. Every other migration grants inside its own guard for the same
        # reason.
        _grant_runtime()


def _grant_runtime() -> None:
    """Activity and research get no UPDATE or DELETE.

    Both are records of what was found and what was done. Research is *versioned*
    rather than corrected, and activity is append-only, so a runtime role that can
    rewrite either has a capability with no legitimate use.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT, INSERT ON agent_activity, donor_research TO granada_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    if _is_postgres():
        for table, constraint in (
            ("donor_research", "fk_research_agent_org"),
            ("agent_activity", "fk_activity_agent_org"),
            ("agent_workflows", "fk_workflow_agent_org"),
            ("jobs", "fk_jobs_agent_org"),
        ):
            op.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS {constraint}')
    op.drop_index("ix_agents_id_org", table_name="granada_agents")
    with op.batch_alter_table("opportunities") as batch:
        batch.drop_column("version")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_workflow")
        batch.drop_column("started_executing_at")
        batch.drop_column("workflow_id")
        batch.drop_column("agent_version")
    op.drop_index("ix_research_current", table_name="donor_research")
    op.drop_table("donor_research")
    op.drop_index("ix_activity_customer", table_name="agent_activity")
    op.drop_index("ix_activity_agent_time", table_name="agent_activity")
    op.drop_table("agent_activity")
