"""Granada Mail: the canonical mail schema (Phase 7a).

Revision ID: 013_mail
Revises: 012_fleet_execution

Everything here follows the pattern established by 011 and 012, deliberately:
a composite ``(agent_id, org_id)`` foreign key, RLS enabled AND forced with four
policies, and a runtime grant applied inside the PostgreSQL branch.

**One row deliberately has no composite key**, and the reason matters.
``mail_provider_events`` has no ``agent_id`` at all: a webhook arrives before
Granada knows which organisation or agent it belongs to - that identification is
what processing *does*. So ``org_id`` is nullable there, and its uniqueness comes
from the provider's own event id instead. Adding an agent column would mean
inventing one, and an invented agent is exactly the forged-identity problem the
composite key exists to prevent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013_mail"
down_revision = "012_fleet_execution"
branch_labels = None
depends_on = None


#: Tables that carry BOTH agent_id and org_id, and therefore get the composite
#: foreign key that makes "org A, agent B" unrepresentable.
AGENT_SCOPED = (
    "mail_accounts",
    "mail_identities",
    "mail_threads",
    "mail_messages",
    "mail_attachments",
    "mail_application_links",
    "mail_classifications",
    "mail_deadlines",
    "mail_drafts",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # mail_accounts
    # ------------------------------------------------------------------
    op.create_table(
        "mail_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("provider_account_id", sa.String(255), nullable=False),
        sa.Column("connection_type", sa.String(30), nullable=False),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200)),
        sa.Column("status", sa.String(20), nullable=False, server_default="CONNECTING"),
        sa.Column("scopes", sa.JSON),
        # A REFERENCE to the secret store. There is deliberately no column that
        # could hold a provider password.
        sa.Column("credentials_ref", sa.String(255)),
        sa.Column("sync_cursor", sa.String(500)),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("sync_status", sa.String(40)),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("provider", "provider_account_id", name="uq_mail_account_provider"),
    )
    op.create_index("ix_mail_accounts_org_id", "mail_accounts", ["org_id"])
    op.create_index("ix_mail_accounts_agent_id", "mail_accounts", ["agent_id"])
    op.create_index("ix_mail_accounts_address", "mail_accounts", ["address"])
    op.create_index("ix_mail_accounts_status", "mail_accounts", ["status"])
    op.create_index("ix_mail_accounts_last_sync_at", "mail_accounts", ["last_sync_at"])

    # ------------------------------------------------------------------
    # mail_identities
    # ------------------------------------------------------------------
    op.create_table(
        "mail_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("mail_account_id", sa.String(36), sa.ForeignKey("mail_accounts.id")),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200)),
        sa.Column("identity_type", sa.String(20), nullable=False),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        # The opaque alias token. Random, not enumerable, revocable.
        sa.Column("token", sa.String(64)),
        sa.Column("purpose_type", sa.String(20)),
        sa.Column("purpose_id", sa.String(36)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # A managed Granada address must be globally unique, and so must a token:
        # two applications resolving to the same alias is precisely the
        # mis-linkage the brief calls worse than no linkage.
        sa.UniqueConstraint("address", name="uq_mail_identity_address"),
        sa.UniqueConstraint("token", name="uq_mail_identity_token"),
    )
    op.create_index("ix_mail_identities_org_id", "mail_identities", ["org_id"])
    op.create_index("ix_mail_identities_agent_id", "mail_identities", ["agent_id"])
    op.create_index("ix_mail_identities_address", "mail_identities", ["address"])
    op.create_index("ix_mail_identities_token", "mail_identities", ["token"])
    op.create_index("ix_mail_identities_identity_type", "mail_identities", ["identity_type"])
    op.create_index("ix_mail_identities_is_primary", "mail_identities", ["is_primary"])
    op.create_index("ix_mail_identities_status", "mail_identities", ["status"])
    op.create_index("ix_mail_identities_purpose_id", "mail_identities", ["purpose_id"])

    # ------------------------------------------------------------------
    # mail_threads
    # ------------------------------------------------------------------
    op.create_table(
        "mail_threads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("mail_account_id", sa.String(36), sa.ForeignKey("mail_accounts.id")),
        sa.Column("provider_thread_id", sa.String(255)),
        sa.Column("normalized_subject", sa.String(500)),
        sa.Column("application_id", sa.String(36)),
        sa.Column("opportunity_id", sa.String(36)),
        sa.Column("donor_id", sa.String(36)),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("first_message_at", sa.DateTime(timezone=True)),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "mail_account_id", "provider_thread_id", name="uq_thread_account_provider"
        ),
    )
    op.create_index("ix_mail_threads_org_id", "mail_threads", ["org_id"])
    op.create_index("ix_mail_threads_agent_id", "mail_threads", ["agent_id"])
    op.create_index("ix_mail_threads_application_id", "mail_threads", ["application_id"])
    op.create_index("ix_mail_threads_last_message_at", "mail_threads", ["last_message_at"])
    op.create_index("ix_mail_threads_status", "mail_threads", ["status"])
    # Deliberately NOT unique on normalized_subject: a subject is not an identity.
    op.create_index("ix_mail_threads_normalized_subject", "mail_threads", ["normalized_subject"])

    # ------------------------------------------------------------------
    # mail_messages
    # ------------------------------------------------------------------
    op.create_table(
        "mail_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("mail_account_id", sa.String(36), sa.ForeignKey("mail_accounts.id")),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("mail_threads.id")),
        sa.Column("provider_message_id", sa.String(255), nullable=False),
        sa.Column("internet_message_id", sa.String(500)),
        sa.Column("in_reply_to", sa.String(500)),
        sa.Column("references", sa.JSON),
        sa.Column("direction", sa.String(10), nullable=False, server_default="INBOUND"),
        sa.Column("sender", sa.String(320)),
        sa.Column("sender_name", sa.String(320)),
        sa.Column("recipients", sa.JSON),
        sa.Column("subject", sa.String(1000)),
        sa.Column("body_ref", sa.String(500)),
        sa.Column("body_preview", sa.Text),
        sa.Column("provider_payload_ref", sa.String(500)),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("authentication_results", sa.JSON),
        sa.Column("processing_status", sa.String(20), nullable=False, server_default="RECEIVED"),
        sa.Column("processing_error", sa.Text),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # Provider-event dedupe alone is not enough: the same message can arrive
        # through two different events, and this is what makes it one row.
        sa.UniqueConstraint(
            "mail_account_id", "provider_message_id", name="uq_message_account_provider"
        ),
    )
    op.create_index("ix_mail_messages_org_id", "mail_messages", ["org_id"])
    op.create_index("ix_mail_messages_agent_id", "mail_messages", ["agent_id"])
    op.create_index("ix_mail_messages_thread_id", "mail_messages", ["thread_id"])
    op.create_index("ix_mail_messages_internet_message_id", "mail_messages", ["internet_message_id"])
    op.create_index("ix_mail_messages_in_reply_to", "mail_messages", ["in_reply_to"])
    op.create_index("ix_mail_messages_sender", "mail_messages", ["sender"])
    op.create_index("ix_mail_messages_received_at", "mail_messages", ["received_at"])
    op.create_index("ix_mail_messages_processing_status", "mail_messages", ["processing_status"])
    op.create_index("ix_mail_messages_direction", "mail_messages", ["direction"])
    op.create_index("ix_mail_messages_correlation_id", "mail_messages", ["correlation_id"])

    # ------------------------------------------------------------------
    # mail_provider_events
    # ------------------------------------------------------------------
    # No agent_id and a nullable org_id: a webhook arrives before the tenant is
    # known. Identity comes from the provider's own event id.
    op.create_table(
        "mail_provider_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("provider_event_id", sa.String(255), nullable=False),
        sa.Column("mail_account_id", sa.String(36), sa.ForeignKey("mail_accounts.id")),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id")),
        sa.Column("event_type", sa.String(80)),
        sa.Column("payload_ref", sa.String(500)),
        sa.Column("status", sa.String(20), nullable=False, server_default="RECEIVED"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("provider", "provider_event_id", name="uq_mail_provider_event"),
    )
    op.create_index("ix_mail_provider_events_provider", "mail_provider_events", ["provider"])
    op.create_index("ix_mail_provider_events_mail_account_id", "mail_provider_events", ["mail_account_id"])
    op.create_index("ix_mail_provider_events_org_id", "mail_provider_events", ["org_id"])
    op.create_index("ix_mail_provider_events_event_type", "mail_provider_events", ["event_type"])
    op.create_index("ix_mail_provider_events_status", "mail_provider_events", ["status"])
    op.create_index("ix_mail_provider_events_received_at", "mail_provider_events", ["received_at"])

    # ------------------------------------------------------------------
    # mail_attachments
    # ------------------------------------------------------------------
    op.create_table(
        "mail_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("mail_messages.id"), nullable=False),
        sa.Column("filename", sa.String(500)),
        sa.Column("mime_type", sa.String(200)),
        sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("checksum_sha256", sa.String(64)),
        sa.Column("storage_ref", sa.String(500)),
        sa.Column("scan_status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("scan_detail", sa.Text),
        # Set ONLY after a human chose to import it into the vault.
        sa.Column("vault_document_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "message_id", "checksum_sha256", "filename", name="uq_attachment_message"
        ),
    )
    op.create_index("ix_mail_attachments_org_id", "mail_attachments", ["org_id"])
    op.create_index("ix_mail_attachments_agent_id", "mail_attachments", ["agent_id"])
    op.create_index("ix_mail_attachments_message_id", "mail_attachments", ["message_id"])
    op.create_index("ix_mail_attachments_checksum", "mail_attachments", ["checksum_sha256"])
    op.create_index("ix_mail_attachments_scan_status", "mail_attachments", ["scan_status"])
    op.create_index("ix_mail_attachments_vault_document_id", "mail_attachments", ["vault_document_id"])

    # ------------------------------------------------------------------
    # mail_application_links
    # ------------------------------------------------------------------
    op.create_table(
        "mail_application_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("mail_messages.id"), nullable=False),
        sa.Column("application_id", sa.String(36)),
        sa.Column("link_method", sa.String(40), nullable=False),
        sa.Column("confidence", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("signals", sa.JSON),
        sa.Column("candidates", sa.JSON),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("corrected_by", sa.String(36)),
        sa.Column("corrected_at", sa.DateTime(timezone=True)),
        # Appendix-only correction: the earlier link is preserved rather than
        # rewritten, so what Granada believed and why survives the override.
        sa.Column("superseded_by", sa.String(36)),
    )
    op.create_index("ix_mail_application_links_org_id", "mail_application_links", ["org_id"])
    op.create_index("ix_mail_application_links_agent_id", "mail_application_links", ["agent_id"])
    op.create_index("ix_mail_application_links_message_id", "mail_application_links", ["message_id"])
    op.create_index("ix_mail_application_links_application_id", "mail_application_links", ["application_id"])
    op.create_index("ix_mail_application_links_confidence", "mail_application_links", ["confidence"])
    op.create_index("ix_mail_application_links_status", "mail_application_links", ["status"])
    op.create_index("ix_mail_application_links_link_method", "mail_application_links", ["link_method"])

    # ------------------------------------------------------------------
    # mail_classifications
    # ------------------------------------------------------------------
    op.create_table(
        "mail_classifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("mail_messages.id"), nullable=False),
        sa.Column("classification", sa.String(40), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float),
        sa.Column("rule_hits", sa.JSON),
        sa.Column("decision_id", sa.String(36)),
        sa.Column("shadow_classification", sa.JSON),
        sa.Column("security_flags", sa.JSON),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mail_classifications_org_id", "mail_classifications", ["org_id"])
    op.create_index("ix_mail_classifications_agent_id", "mail_classifications", ["agent_id"])
    op.create_index("ix_mail_classifications_message_id", "mail_classifications", ["message_id"])
    op.create_index("ix_mail_classifications_classification", "mail_classifications", ["classification"])
    op.create_index("ix_mail_classifications_method", "mail_classifications", ["method"])
    op.create_index("ix_mail_classifications_decision_id", "mail_classifications", ["decision_id"])
    op.create_index("ix_mail_classifications_classified_at", "mail_classifications", ["classified_at"])

    # ------------------------------------------------------------------
    # mail_deadlines
    # ------------------------------------------------------------------
    op.create_table(
        "mail_deadlines",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("mail_messages.id"), nullable=False),
        sa.Column("application_id", sa.String(36)),
        sa.Column("raw_expression", sa.String(500), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("timezone_assumption", sa.String(60)),
        sa.Column("confidence", sa.Float),
        sa.Column("status", sa.String(20), nullable=False, server_default="RESOLVED"),
        sa.Column("resolved_by", sa.String(60)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mail_deadlines_org_id", "mail_deadlines", ["org_id"])
    op.create_index("ix_mail_deadlines_agent_id", "mail_deadlines", ["agent_id"])
    op.create_index("ix_mail_deadlines_message_id", "mail_deadlines", ["message_id"])
    op.create_index("ix_mail_deadlines_application_id", "mail_deadlines", ["application_id"])
    op.create_index("ix_mail_deadlines_resolved_at", "mail_deadlines", ["resolved_at"])
    op.create_index("ix_mail_deadlines_status", "mail_deadlines", ["status"])
    op.create_index("ix_mail_deadlines_created_at", "mail_deadlines", ["created_at"])

    # ------------------------------------------------------------------
    # mail_drafts
    # ------------------------------------------------------------------
    op.create_table(
        "mail_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("granada_agents.id"), nullable=False),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("mail_threads.id")),
        sa.Column("application_id", sa.String(36)),
        sa.Column("reply_to_message_id", sa.String(36), sa.ForeignKey("mail_messages.id")),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("subject", sa.String(1000)),
        sa.Column("body", sa.Text),
        sa.Column("status", sa.String(20), nullable=False, server_default="GENERATING"),
        sa.Column("status_reason", sa.Text),
        sa.Column("model_invocation_id", sa.String(36)),
        sa.Column("prompt_version", sa.String(50)),
        sa.Column("facts_used", sa.JSON),
        sa.Column("documents_used", sa.JSON),
        sa.Column("organisation_profile_version", sa.Integer),
        sa.Column("application_version", sa.Integer),
        sa.Column("research_version", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("approved_by", sa.String(36)),
        # Present so the schema can describe the future state honestly, and never
        # written during Phase 7a.
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "reply_to_message_id", "application_version", "research_version", "version",
            name="uq_draft_message_revision",
        ),
    )
    op.create_index("ix_mail_drafts_org_id", "mail_drafts", ["org_id"])
    op.create_index("ix_mail_drafts_agent_id", "mail_drafts", ["agent_id"])
    op.create_index("ix_mail_drafts_application_id", "mail_drafts", ["application_id"])
    op.create_index("ix_mail_drafts_thread_id", "mail_drafts", ["thread_id"])
    op.create_index("ix_mail_drafts_reply_to_message_id", "mail_drafts", ["reply_to_message_id"])
    op.create_index("ix_mail_drafts_status", "mail_drafts", ["status"])
    op.create_index("ix_mail_drafts_created_at", "mail_drafts", ["created_at"])
    op.create_index("ix_mail_drafts_model_invocation_id", "mail_drafts", ["model_invocation_id"])

    if not _is_postgres():
        return

    # ------------------------------------------------------------------
    # PostgreSQL-only: composite invariant, RLS, grants
    # ------------------------------------------------------------------
    # The same invariant as the fleet: a mail row must not be able to say
    # "org A, agent B". MATCH SIMPLE means a NULL agent skips the check, which is
    # right here because every one of these columns is NOT NULL anyway.
    for table in AGENT_SCOPED:
        op.execute(
            f"""
            ALTER TABLE {table}
              ADD CONSTRAINT fk_{table}_agent_org
              FOREIGN KEY (agent_id, org_id)
              REFERENCES granada_agents (id, org_id)
            """
        )

    # RLS, enabled AND forced, with the same four policies as every other
    # organisation-owned table. FORCE matters: it binds the table owner, so a
    # migration or a maintenance script cannot quietly read across tenants.
    org_scoped = (*AGENT_SCOPED, "mail_provider_events")
    for table in org_scoped:
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

    # A provider event may legitimately have a NULL org_id (the tenant is not known
    # until the mailbox is resolved), so its insert policy must allow that rather
    # than making the webhook path impossible.
    op.execute("DROP POLICY mail_provider_events_insert ON mail_provider_events")
    op.execute(
        "CREATE POLICY mail_provider_events_insert ON mail_provider_events FOR INSERT"
        " WITH CHECK (org_id IS NULL OR org_id = app.current_org())"
    )

    _grant_runtime()


def _grant_runtime() -> None:
    """SELECT/INSERT/UPDATE for the runtime role; DELETE withheld.

    Mail is evidence. A message, a classification, a deadline or a draft that the
    application role can delete is a record that can be made to disappear, so the
    runtime role is granted exactly what it needs and the DELETE is left to the
    owner - the same discipline as the fleet's append-only tables.

    **Grants are conditional on the role existing.** A hard ``GRANT ... TO
    granada_replica`` aborted this entire migration in an environment where the
    replica role had not been created, which is a bad failure mode: a schema change
    should not depend on an operational role that some deployments do not have. The
    DO block grants to whichever of the expected roles are actually present, so the
    migration applies everywhere and the privilege is still correct where the role
    exists.
    """
    tables = (
        "mail_accounts", "mail_identities", "mail_threads", "mail_messages",
        "mail_provider_events", "mail_attachments", "mail_application_links",
        "mail_classifications", "mail_deadlines", "mail_drafts",
    )
    # Never editable in place: they record what Granada concluded.
    append_only = ("mail_classifications", "mail_application_links")

    op.execute("GRANT USAGE ON SCHEMA public TO granada_app")
    for table in tables:
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO granada_app")
        op.execute(f"REVOKE DELETE ON TABLE {table} FROM granada_app")
    for table in append_only:
        op.execute(f"REVOKE UPDATE ON TABLE {table} FROM granada_app")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_replica') THEN
                GRANT USAGE ON SCHEMA public TO granada_replica;
                GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO granada_replica;
                REVOKE UPDATE ON TABLE mail_classifications FROM granada_replica;
                REVOKE UPDATE ON TABLE mail_application_links FROM granada_replica;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    for table in (
        "mail_drafts", "mail_deadlines", "mail_classifications",
        "mail_application_links", "mail_attachments", "mail_provider_events",
        "mail_messages", "mail_threads", "mail_identities", "mail_accounts",
    ):
        op.drop_table(table)
