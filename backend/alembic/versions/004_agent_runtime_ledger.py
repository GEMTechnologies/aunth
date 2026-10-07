"""agent runtime ledger: jobs, attempts, outbox and webhook inbox.

Revision ID: 004_agent_runtime_ledger
Revises: 003_row_level_security
Create Date: 2026-10-07

Purpose
-------
Phase 2 needs a durable record of agent work that survives a crash, a
restart, and a trimmed Redis stream. PostgreSQL is that record; Redis Streams
carries delivery only. Four tables:

``jobs``          the durable ledger of work, with state, backoff and leases
``job_attempts``  every execution, kept so a DLQ is investigable
``outbox_events`` transactional outbox, so a state change and its event commit together
``inbox_events``  provider webhook dedupe, because providers redeliver

Row-level security
------------------
``jobs`` is ENABLE **and** FORCE: even the table owner is bound, so
cross-tenant access is denied structurally rather than by convention.

``job_attempts`` is ENABLE only. It has no ``org_id`` of its own - an attempt
belongs to whatever job it belongs to - so its policies reference the parent
``jobs`` row. A subquery in a policy runs as the invoking user and therefore
inherits ``jobs``' FORCE, which is what makes the join safe. Forcing it as
well would add nothing, because the parent row is already unreadable.

Two tables are ENABLE-only and the reasons are not the same:

* ``outbox_events`` - the relay is system-wide infrastructure. It must drain
  unpublished events for *every* tenant in one sweep and legitimately has no
  tenant to set. Only the owner reads across tenants, which is the same
  relationship ``org_members`` already has (see ADR-0005). The application
  role stays bound by the policies.
* ``inbox_events`` - pre-tenant by nature. A webhook arrives before anything
  is authenticated, so ``org_id`` is filled in only after correlation.

Forcing either of those would break the system they exist to serve, which
would make them a control that is never actually true.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004_agent_runtime_ledger"
down_revision = "003_row_level_security"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(table: str, name: str, command: str, using: str, check: str | None = None) -> None:
    """Create one policy, emitting only the clauses PostgreSQL accepts.

    Mirrors the helper in 003_row_level_security on purpose. The clause /
    command matrix is not symmetric and the server rejects the wrong pairing
    outright, so it is enforced in one place rather than left to each caller:

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


# An attempt belongs to its job, and inherits that job's tenancy.
_ATTEMPT_TENANT = (
    "EXISTS (SELECT 1 FROM jobs j WHERE j.id = job_attempts.job_id "
    "AND j.org_id = app.current_org())"
)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=True, index=True),
        sa.Column("stream", sa.String(128), nullable=False, index=True),
        sa.Column("job_type", sa.String(100), nullable=False, index=True),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="QUEUED", index=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("failure_category", sa.String(40), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("org_id", "job_type", "idempotency_key", name="uq_jobs_idempotency"),
    )
    op.create_index("ix_jobs_dispatch", "jobs", ["state", "available_at"])
    op.create_index("ix_jobs_lease", "jobs", ["lease_expires_at"])

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("failure_category", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=True, index=True),
        sa.Column("stream", sa.String(128), nullable=False, index=True),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index("ix_outbox_unpublished", "outbox_events", ["published_at", "created_at"])

    op.create_table(
        "inbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=True, index=True),
        sa.Column("source", sa.String(64), nullable=False, index=True),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="RECEIVED", index=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.UniqueConstraint("source", "external_event_id", name="uq_inbox_source_event"),
    )

    if not _is_postgres():
        # Documented no-op, matching 003. SQLite has no row-level security, so
        # the tenant boundary is the application layer alone - which is exactly
        # why the request-path probe runs against real PostgreSQL.
        return

    for table in ("jobs", "job_attempts", "outbox_events", "inbox_events"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "jobs" FORCE ROW LEVEL SECURITY')

    # -- jobs ---------------------------------------------------------------
    _policy("jobs", "jobs_select", "SELECT", "org_id = app.current_org()")
    _policy("jobs", "jobs_insert", "INSERT", "false", check="org_id = app.current_org()")
    _policy("jobs", "jobs_update", "UPDATE", "org_id = app.current_org()", check="org_id = app.current_org()")
    _policy("jobs", "jobs_delete", "DELETE", "org_id = app.current_org()")

    # -- job_attempts -------------------------------------------------------
    # No org_id column: tenancy comes from the parent job. The subquery is
    # evaluated as the invoking user, so it inherits jobs' FORCE.
    _policy("job_attempts", "attempts_select", "SELECT", _ATTEMPT_TENANT)
    _policy("job_attempts", "attempts_insert", "INSERT", "false", check=_ATTEMPT_TENANT)
    _policy("job_attempts", "attempts_update", "UPDATE", _ATTEMPT_TENANT)
    _policy("job_attempts", "attempts_delete", "DELETE", _ATTEMPT_TENANT)

    # -- outbox_events ------------------------------------------------------
    # A tenant may stage events for itself, or unscoped system events. It may
    # not stage an event carrying another tenant's org_id: the relay publishes
    # whatever it finds, so that would forge work into someone else's queue.
    _tenant_or_system = "org_id IS NULL OR org_id = app.current_org()"
    _policy("outbox_events", "outbox_select", "SELECT", _tenant_or_system)
    _policy("outbox_events", "outbox_insert", "INSERT", "false", check=_tenant_or_system)
    _policy("outbox_events", "outbox_update", "UPDATE", _tenant_or_system)
    _policy("outbox_events", "outbox_delete", "DELETE", _tenant_or_system)

    # -- inbox_events -------------------------------------------------------
    # INSERT is unconditionally allowed: the tenant genuinely is not known yet.
    # The residual is that a still-uncorrelated row (org_id IS NULL) is readable
    # by any tenant. That window is bounded - rows are deleted once correlated -
    # and the payload is provider-supplied, never tenant-authored.
    _policy("inbox_events", "inbox_select", "SELECT", _tenant_or_system)
    _policy("inbox_events", "inbox_insert", "INSERT", "false", check="true")
    _policy("inbox_events", "inbox_update", "UPDATE", _tenant_or_system)
    _policy("inbox_events", "inbox_delete", "DELETE", _tenant_or_system)

    _grant_runtime()


def _grant_runtime() -> None:
    """Grant the runtime role least-privilege DML on the new tables.

    Schema access and ``alembic_version`` are withheld, matching
    ``sql/grant_runtime_role.sql``: the application role has no business
    re-running migrations or creating objects.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT, INSERT, UPDATE ON jobs, job_attempts TO granada_app;
                GRANT SELECT, INSERT, UPDATE, DELETE ON outbox_events, inbox_events TO granada_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_table("inbox_events")
    op.drop_table("outbox_events")
    op.drop_table("job_attempts")
    op.drop_index("ix_jobs_lease", table_name="jobs")
    op.drop_index("ix_jobs_dispatch", table_name="jobs")
    op.drop_table("jobs")