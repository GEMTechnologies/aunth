"""Notification preferences, notifications and delivery evidence.

Revision ID: 018_notifications
Revises: 017_delivery

Phase 10. Until now every event the platform emits - including `report.overdue`, the alert
with the clearest financial consequence - was published to a Redis stream that **nothing
consumed for a human**. This is the path from an event to a person.

``notification_deliveries`` is append-only for the same reason ``mail_send_attempts`` and
``submission_receipts`` are: without it, "the platform knew and told somebody" is an
assertion rather than a fact.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "018_notifications"
down_revision = "017_delivery"
branch_labels = None
depends_on = None

TABLES = ("notification_preferences", "notifications", "notification_deliveries")

#: Evidence that must not be rewritable or erasable.
APPEND_ONLY = ("notification_deliveries",)

#: `notifications` has a lifecycle - UNREAD to READ to ACTIONED - so UPDATE is correct.
#: But DELETE is not: a notification that was raised is evidence the platform knew.
DELETE_REVOKED_UPDATE_ALLOWED = ("notifications", "notification_preferences")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, server_default="IN_APP"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("min_severity", sa.String(20), nullable=False, server_default="INFO"),
        sa.Column("quiet_from_hour", sa.Integer),
        sa.Column("quiet_to_hour", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("org_id", "user_id", "category", "channel",
                            name="uq_notification_preference"),
    )
    for column in ("org_id", "user_id", "category", "channel", "enabled"):
        op.create_index(f"ix_notification_preferences_{column}",
                        "notification_preferences", [column])

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36)),
        sa.Column("user_id", sa.String(36)),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False, server_default="INFO"),
        sa.Column("status", sa.String(20), nullable=False, server_default="UNREAD"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text),
        sa.Column("action_required", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("action_url", sa.String(1000)),
        # The suppression identity. Built from the SUBJECT and the CONDITION rather than
        # from the event id, because a scan that runs every thirty seconds would otherwise
        # raise a new notification every thirty seconds.
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("source_event_type", sa.String(80)),
        sa.Column("source_event_id", sa.String(36)),
        sa.Column("context", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("repeat_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "user_id", "dedupe_key",
                            name="uq_notification_dedupe"),
    )
    for column in ("org_id", "agent_id", "user_id", "category", "severity", "status",
                   "action_required", "dedupe_key", "source_event_type",
                   "source_event_id", "created_at"):
        op.create_index(f"ix_notifications_{column}", "notifications", [column])

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("notification_id", sa.String(36),
                  sa.ForeignKey("notifications.id"), nullable=False),
        sa.Column("user_id", sa.String(36)),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(255)),
        sa.Column("provider_reference", sa.String(255)),
        sa.Column("error_code", sa.String(60)),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("org_id", "notification_id", "user_id", "channel", "result",
                   "attempted_at"):
        op.create_index(f"ix_notification_deliveries_{column}",
                        "notification_deliveries", [column])

    if not _is_postgres():
        return

    for table in TABLES:
        op.execute(
            f"""
            ALTER TABLE {table}
              ADD CONSTRAINT fk_{table}_org
              FOREIGN KEY (org_id) REFERENCES organisations (id)
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
    """UPDATE where a lifecycle exists, DELETE revoked everywhere.

    The REVOKE lives in the migration as well as in ``sql/grant_runtime_role.sql``, because
    that script is additive and has silently re-granted privileges on an append-only table
    **nine times** in this project. Establishing the posture here means a fresh deployment
    is correct before the script is ever re-run.
    """
    op.execute("GRANT USAGE ON SCHEMA public TO granada_app")
    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO granada_app")
        op.execute(f"REVOKE DELETE ON TABLE {table} FROM granada_app")

    # A delivery record is evidence. Evidence may not be rewritten.
    for table in APPEND_ONLY:
        op.execute(f"REVOKE UPDATE ON TABLE {table} FROM granada_app")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_replica') THEN
                GRANT USAGE ON SCHEMA public TO granada_replica;
                GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO granada_replica;
                REVOKE DELETE ON TABLE notification_preferences FROM granada_replica;
                REVOKE DELETE ON TABLE notifications FROM granada_replica;
                REVOKE DELETE ON TABLE notification_deliveries FROM granada_replica;
                REVOKE UPDATE ON TABLE notification_deliveries FROM granada_replica;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
