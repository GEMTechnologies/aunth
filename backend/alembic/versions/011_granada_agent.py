"""the persistent Granada Agent: one logical agent per organisation.

Revision ID: 011_granada_agent
Revises: 010_application_workspace
Create Date: 2026-10-07

Purpose
-------
Introduce the entity the product actually promises: *create your profile once;
Granada creates your agent; your agent works for you continuously.*

The architectural correction this makes
---------------------------------------
The unit of autonomy is **the agent**, not the workflow and not the job. Before
this revision a workflow was anonymous work; afterwards every autonomous workflow
belongs to a persistent agent, and "whose memory am I acting on" has one answer
that is not inferred from whichever request is in flight.

It also fixes the scaling model in the schema rather than in a document:

    10,000 NGOs -> 10,000 logical agents -> ONE shared worker platform

and explicitly **not** 10,000 permanently running processes. There is no process,
thread, scheduler or Redis lock per agent anywhere in this design. What exists is
`jobs.agent_id`: a worker picks up a job, loads the agent named on it, does the
work, and becomes available for another organisation.

Three tables and one column:

``granada_agents``    one per organisation - unique, so "one agent per NGO" is a
                      constraint rather than a convention
``agent_specialists`` the named parts (Opportunity Hunter, Proposal Writer, ...)
                      so the customer sees what their agent is doing rather than
                      an opaque worker
``agent_workflows``   durable workflow instances belonging to an agent, with
                      ``next_run_at`` as the wake-up rather than a sleeping process
``jobs.agent_id``     which agent a unit of work belongs to. Nullable: system work
                      (the outbox relay, an uncorrelated webhook) belongs to no
                      agent, and inventing one would attribute work to a customer
                      who did not ask for it.

Row-level security
------------------
ENABLE and FORCE on all three. An agent row states which organisation is being
represented, what it is authorised to do and what it is currently working on -
that is the customer's most sensitive operational data, and it is scoped by
``org_id`` like everything else.

``agent_workflows`` keeps a tenant ``org_id`` alongside ``agent_id`` rather than
reaching tenancy through the agent, for the same reason
``application_transitions`` does: the dispatcher reads it across agents, and a
subquery policy would make the hottest queue query the slowest.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "011_granada_agent"
down_revision = "010_application_workspace"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(table: str, name: str, command: str, using: str, check: str | None = None) -> None:
    """Mirrors the helper in 003-010; see 003 for the clause/command matrix."""
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
        "granada_agents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False, index=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("vertical", sa.String(20), nullable=False, server_default="NGO", index=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PROVISIONING", index=True),
        # The authority ceiling for every specialist under this agent.
        sa.Column("autonomy", sa.String(30), nullable=False, server_default="MONITOR_ONLY", index=True),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("org_id", name="uq_agent_org"),
    )

    op.create_table(
        "agent_specialists",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False, index=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False, index=True),
        sa.Column("key", sa.String(40), nullable=False, index=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="IDLE", index=True),
        sa.Column("current_activity", sa.String(255), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runs_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("agent_id", "key", name="uq_specialist_agent_key"),
    )
    op.create_index(
        "ix_specialists_agent_status", "agent_specialists", ["agent_id", "status"]
    )

    op.create_table(
        "agent_workflows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False, index=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False, index=True),
        sa.Column("specialist_key", sa.String(40), nullable=True, index=True),
        sa.Column("workflow_type", sa.String(60), nullable=False, index=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="PENDING", index=True),
        sa.Column("subject_type", sa.String(20), nullable=True, index=True),
        sa.Column("subject_id", sa.String(36), nullable=True, index=True),
        # The wake-up. A workflow that must act in three days is scheduled, not
        # held open by a process.
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waiting_on", sa.String(255), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100", index=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        # One workflow per (agent, type, subject): two identical workflows would
        # mean the same opportunity pursued twice.
        sa.UniqueConstraint(
            "agent_id", "workflow_type", "subject_type", "subject_id",
            name="uq_workflow_agent_subject",
        ),
    )
    op.create_index("ix_workflows_due", "agent_workflows", ["state", "next_run_at"])
    op.create_index(
        "ix_workflows_agent_state", "agent_workflows", ["agent_id", "state"]
    )

    # -- jobs.agent_id ------------------------------------------------------
    # Nullable and NOT backfilled: existing jobs were system work or pre-agent
    # work, and assigning them to an agent now would attribute work to a customer
    # who did not ask for it. `batch_alter_table` so SQLite can add the column
    # and its constraint.
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("agent_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_jobs_agent_id", "granada_agents", ["agent_id"], ["id"]
        )
        batch.create_index("ix_jobs_agent_id", ["agent_id"])
    op.create_index("ix_jobs_agent_dispatch", "jobs", ["agent_id", "state", "available_at"])

    if not _is_postgres():
        return

    for table in ("granada_agents", "agent_specialists", "agent_workflows"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        tenant = "org_id = app.current_org()"
        _policy(table, f"{table}_select", "SELECT", tenant)
        _policy(table, f"{table}_insert", "INSERT", "false", check=tenant)
        _policy(table, f"{table}_update", "UPDATE", tenant, check=tenant)
        # An agent is a configuration object, not evidence. Deleting one is a
        # deliberate act (an organisation leaving), and the ledger, the vault and
        # the decision history survive it because they are separate tables with
        # their own restrictions.
        _policy(table, f"{table}_delete", "DELETE", tenant)

    _grant_runtime()


def _grant_runtime() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON granada_agents, agent_specialists, agent_workflows
                    TO granada_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_agent_dispatch", table_name="jobs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_agent_id")
        batch.drop_constraint("fk_jobs_agent_id", type_="foreignkey")
        batch.drop_column("agent_id")
    op.drop_index("ix_workflows_agent_state", table_name="agent_workflows")
    op.drop_index("ix_workflows_due", table_name="agent_workflows")
    op.drop_table("agent_workflows")
    op.drop_index("ix_specialists_agent_status", table_name="agent_specialists")
    op.drop_table("agent_specialists")
    op.drop_table("granada_agents")
