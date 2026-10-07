"""Outbound mail: immutable send intents, approvals, append-only attempts.

Revision ID: 014_outbound_mail
Revises: 013_mail

Three tables and one invariant each:

``mail_send_intents``
    The frozen message. Its ``message_fingerprint`` is what a human approves, so
    editing a draft after approval cannot change what is sent.
``mail_approvals``
    A decision bound to one fingerprint. Records *which* fingerprint, not a
    boolean.
``mail_send_attempts``
    **Append-only.** UPDATE and DELETE are withheld from the runtime role, and the
    posture is verified live - the additive ``ALTER DEFAULT PRIVILEGES`` trap has
    silently re-granted those privileges on an append-only table five times here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "014_outbound_mail"
down_revision = "013_mail"
branch_labels = None
depends_on = None

#: Tables carrying both agent_id and org_id, so the composite invariant applies.
AGENT_SCOPED = ("mail_send_intents", "mail_approvals", "mail_send_attempts")

#: Tables whose history must never be rewritten.
APPEND_ONLY = ("mail_send_attempts", "mail_approvals")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # mail_send_intents
    # ------------------------------------------------------------------
    op.create_table(
        "mail_send_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("mail_account_id", sa.String(36), sa.ForeignKey("mail_accounts.id")),
        sa.Column("mail_identity_id", sa.String(36), sa.ForeignKey("mail_identities.id")),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("mail_threads.id")),
        sa.Column("application_id", sa.String(36)),
        sa.Column("reply_to_message_id", sa.String(36), sa.ForeignKey("mail_messages.id")),
        sa.Column("draft_id", sa.String(36), sa.ForeignKey("mail_drafts.id")),
        sa.Column("draft_version", sa.Integer),
        sa.Column("from_address", sa.String(320)),
        sa.Column("to_addresses", sa.JSON),
        sa.Column("cc_addresses", sa.JSON),
        sa.Column("bcc_addresses", sa.JSON),
        sa.Column("reply_to_address", sa.String(320)),
        sa.Column("subject", sa.String(1000)),
        sa.Column("body_snapshot", sa.Text),
        sa.Column("attachment_manifest", sa.JSON),
        sa.Column("message_fingerprint", sa.String(64), nullable=False),
        sa.Column("fingerprint_input", sa.Text),
        sa.Column("risk_class", sa.String(40), nullable=False),
        sa.Column("risk_detail", sa.JSON),
        sa.Column("status", sa.String(30), nullable=False, server_default="WAITING_FOR_APPROVAL"),
        sa.Column("status_reason", sa.Text),
        sa.Column("approval_request_id", sa.String(36)),
        sa.Column("agent_version", sa.Integer),
        sa.Column("provider", sa.String(40)),
        sa.Column("provider_submission_id", sa.String(255)),
        sa.Column("provider_message_id", sa.String(255)),
        sa.Column("internet_message_id", sa.String(500)),
        sa.Column("granada_message_ref", sa.String(64)),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("send_started_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_state", sa.String(30)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("bounced_at", sa.DateTime(timezone=True)),
        sa.Column("bounce_detail", sa.JSON),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("retry_not_before", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(60)),
        sa.Column("failure_summary", sa.Text),
        sa.Column("reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("reconciliation_state", sa.String(40)),
        sa.Column("correlation_id", sa.String(64)),
        # ONE approved intent is ONE logical donor email. This is the guarantee; a
        # Redis lock is not, because it dies with the connection that took it.
        sa.UniqueConstraint("idempotency_key", name="uq_send_intent_idempotency"),
    )
    op.create_index("ix_mail_send_intents_org_id", "mail_send_intents", ["org_id"])
    op.create_index("ix_mail_send_intents_agent_id", "mail_send_intents", ["agent_id"])
    op.create_index("ix_mail_send_intents_status", "mail_send_intents", ["status"])
    op.create_index("ix_mail_send_intents_risk_class", "mail_send_intents", ["risk_class"])
    op.create_index("ix_mail_send_intents_fingerprint", "mail_send_intents", ["message_fingerprint"])
    op.create_index("ix_mail_send_intents_application_id", "mail_send_intents", ["application_id"])
    op.create_index("ix_mail_send_intents_thread_id", "mail_send_intents", ["thread_id"])
    op.create_index("ix_mail_send_intents_created_at", "mail_send_intents", ["created_at"])
    op.create_index("ix_mail_send_intents_sent_at", "mail_send_intents", ["sent_at"])
    op.create_index("ix_mail_send_intents_delivery_state", "mail_send_intents", ["delivery_state"])
    op.create_index("ix_mail_send_intents_provider", "mail_send_intents", ["provider"])
    op.create_index("ix_mail_send_intents_approval_request_id", "mail_send_intents", ["approval_request_id"])
    op.create_index("ix_mail_send_intents_provider_submission_id", "mail_send_intents", ["provider_submission_id"])
    op.create_index("ix_mail_send_intents_granada_message_ref", "mail_send_intents", ["granada_message_ref"])
    op.create_index("ix_mail_send_intents_correlation_id", "mail_send_intents", ["correlation_id"])
    op.create_index("ix_mail_send_intents_draft_id", "mail_send_intents", ["draft_id"])
    op.create_index("ix_mail_send_intents_reply_to", "mail_send_intents", ["reply_to_message_id"])
    op.create_index("ix_mail_send_intents_failure_code", "mail_send_intents", ["failure_code"])

    # ------------------------------------------------------------------
    # mail_approvals
    # ------------------------------------------------------------------
    op.create_table(
        "mail_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("send_intent_id", sa.String(36), sa.ForeignKey("mail_send_intents.id"), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("risk_class", sa.String(40), nullable=False),
        sa.Column("fingerprint_input", sa.Text),
        sa.Column("approved_by", sa.String(36), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("permission_used", sa.String(80)),
        sa.Column("membership_id", sa.String(36)),
        sa.Column("approval_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.String(36)),
        sa.Column("note", sa.Text),
        # One decision per (intent, fingerprint). Re-approving the SAME fingerprint
        # is idempotent; approving a different one is a new row and implicitly
        # leaves the old approval describing something that is no longer the intent.
        sa.UniqueConstraint("send_intent_id", "fingerprint", name="uq_approval_intent_fingerprint"),
    )
    op.create_index("ix_mail_approvals_org_id", "mail_approvals", ["org_id"])
    op.create_index("ix_mail_approvals_agent_id", "mail_approvals", ["agent_id"])
    op.create_index("ix_mail_approvals_send_intent_id", "mail_approvals", ["send_intent_id"])
    op.create_index("ix_mail_approvals_decision", "mail_approvals", ["decision"])
    op.create_index("ix_mail_approvals_fingerprint", "mail_approvals", ["fingerprint"])
    op.create_index("ix_mail_approvals_status", "mail_approvals", ["status"])
    op.create_index("ix_mail_approvals_approved_by", "mail_approvals", ["approved_by"])
    op.create_index("ix_mail_approvals_approved_at", "mail_approvals", ["approved_at"])

    # ------------------------------------------------------------------
    # mail_send_attempts (append-only)
    # ------------------------------------------------------------------
    op.create_table(
        "mail_send_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("send_intent_id", sa.String(36), sa.ForeignKey("mail_send_intents.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("request_fingerprint", sa.String(64)),
        sa.Column("granada_message_ref", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("result", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(60)),
        sa.Column("safe_error_summary", sa.Text),
        sa.Column("provider_submission_id", sa.String(255)),
        sa.Column("provider_message_id", sa.String(255)),
        sa.Column("reconciliation_state", sa.String(20), nullable=False, server_default="UNKNOWN"),
        sa.Column("reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mail_send_attempts_org_id", "mail_send_attempts", ["org_id"])
    op.create_index("ix_mail_send_attempts_agent_id", "mail_send_attempts", ["agent_id"])
    op.create_index("ix_mail_send_attempts_send_intent_id", "mail_send_attempts", ["send_intent_id"])
    op.create_index("ix_mail_send_attempts_result", "mail_send_attempts", ["result"])
    op.create_index("ix_mail_send_attempts_attempt_id", "mail_send_attempts", ["attempt_id"])
    op.create_index("ix_mail_send_attempts_granada_message_ref", "mail_send_attempts", ["granada_message_ref"])
    op.create_index("ix_mail_send_attempts_error_code", "mail_send_attempts", ["error_code"])
    op.create_index("ix_mail_send_attempts_started_at", "mail_send_attempts", ["started_at"])
    op.create_index("ix_mail_send_attempts_created_at", "mail_send_attempts", ["created_at"])
    op.create_index("ix_mail_send_attempts_reconciliation_state", "mail_send_attempts", ["reconciliation_state"])
    op.create_index("ix_mail_send_attempts_provider_submission_id", "mail_send_attempts", ["provider_submission_id"])

    # ------------------------------------------------------------------
    # mail_drafts: human-edit provenance
    # ------------------------------------------------------------------
    # A human edit creates a NEW version rather than overwriting the draft, so the
    # text Granada proposed and the text a person approved are both recoverable.
    op.add_column("mail_drafts", sa.Column("supersedes_id", sa.String(36)))
    op.add_column("mail_drafts", sa.Column("edit_source", sa.String(20)))
    op.add_column("mail_drafts", sa.Column("edited_by", sa.String(36)))
    op.add_column("mail_drafts", sa.Column("edited_at", sa.DateTime(timezone=True)))
    op.create_index("ix_mail_drafts_supersedes_id", "mail_drafts", ["supersedes_id"])

    if not _is_postgres():
        return

    # ------------------------------------------------------------------
    # Composite invariant, RLS, grants
    # ------------------------------------------------------------------
    for table in AGENT_SCOPED:
        op.execute(
            f"""
            ALTER TABLE {table}
              ADD CONSTRAINT fk_{table}_agent_org
              FOREIGN KEY (agent_id, org_id)
              REFERENCES granada_agents (id, org_id)
            """
        )

    for table in AGENT_SCOPED:
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
    """Grants that make the append-only tables genuinely append-only.

    ``mail_send_attempts`` and ``mail_approvals`` are history: an attempt row that
    says "we called the provider and never learned what happened" is the only
    evidence reconciliation and an operator have, and an approval row is the record
    of who authorised what. A row a caller can rewrite is worse than no row, because
    it looks authoritative.

    So UPDATE and DELETE are revoked for the runtime role, **inside the migration**
    rather than only in ``sql/grant_runtime_role.sql``. The central script is
    additive and has silently re-granted these five times; putting the REVOKE in the
    migration as well means the posture is established by the schema change itself.
    """
    tables = ("mail_send_intents", "mail_approvals", "mail_send_attempts")
    op.execute("GRANT USAGE ON SCHEMA public TO granada_app")
    for table in tables:
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO granada_app")
        op.execute(f"REVOKE DELETE ON TABLE {table} FROM granada_app")

    # The append-only pair keeps no UPDATE either. A send intent must stay mutable
    # (its status advances), but its history and its approvals must not.
    for table in APPEND_ONLY:
        op.execute(f"REVOKE UPDATE ON TABLE {table} FROM granada_app")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_replica') THEN
                GRANT USAGE ON SCHEMA public TO granada_replica;
                GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO granada_replica;
                REVOKE UPDATE ON TABLE mail_send_attempts FROM granada_replica;
                REVOKE UPDATE ON TABLE mail_approvals FROM granada_replica;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_mail_drafts_supersedes_id", table_name="mail_drafts")
    for column in ("supersedes_id", "edit_source", "edited_by", "edited_at"):
        op.drop_column("mail_drafts", column)
    for table in ("mail_send_attempts", "mail_approvals", "mail_send_intents"):
        op.drop_table(table)
