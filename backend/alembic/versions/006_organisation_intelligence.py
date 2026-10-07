"""organisation intelligence: the Digital Twin and the document vault.

Revision ID: 006_organisation_intelligence
Revises: 005_model_invocations
Create Date: 2026-10-07

Purpose
-------
Phase 3. Two tables that every later phase reads:

``org_facts``   what Granada knows about an organisation, and *how it knows it*
``documents``   the vault: versioned, checksummed, expiring, approval-gated

Why the fact table looks like this
----------------------------------
The security gate names one failure above the others: **an AI-inferred value
must never silently become a fact in a submitted application.** The schema
prevents that structurally rather than by convention.

``state`` is a closed set that includes ``AI_INFERRED`` explicitly, so
"provenance unknown" is not representable - a fact cannot be written without
saying which kind it is. ``source`` is NOT NULL for the same reason. Versions are
appended and superseded rather than overwritten, so an application submitted
against version 3 stays explainable after version 4 arrives.

``valid_until`` exists because organisation facts genuinely expire. A lapsed
certificate of registration that still reads as current is how stale information
reaches a funder.

Row-level security
------------------
ENABLE and FORCE on both, matching ``jobs`` and ``model_invocations``. These rows
are the organisation's identity, registration numbers, bank details and legal
documents: the most sensitive data in the product, and the data whose disclosure
would be most damaging. Nothing legitimately reads another tenant's.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006_organisation_intelligence"
down_revision = "005_model_invocations"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(table: str, name: str, command: str, using: str, check: str | None = None) -> None:
    """Create one policy, emitting only the clauses PostgreSQL accepts.

    Mirrors the helper in 003, 004 and 005. The clause/command matrix is not
    symmetric and the server rejects the wrong pairing outright, so it is
    enforced in one place rather than left to each caller:

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


def upgrade() -> None:
    op.create_table(
        "org_facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False, index=True),
        sa.Column("key", sa.String(120), nullable=False, index=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("value_type", sa.String(20), nullable=False, server_default="text"),
        sa.Column("state", sa.String(20), nullable=False, index=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("source_ref", sa.String(255), nullable=True),
        sa.Column("evidence_document_id", sa.String(36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("supersedes_id", sa.String(36), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.UniqueConstraint("org_id", "key", "version", name="uq_org_facts_version"),
    )
    op.create_index(
        "ix_org_facts_current", "org_facts", ["org_id", "is_current", "key"]
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organisations.id"), nullable=False, index=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("doc_type", sa.String(80), nullable=False, index=True),
        sa.Column("scope", sa.String(20), nullable=False, server_default="ORGANISATION", index=True),
        sa.Column("scope_ref", sa.String(36), nullable=True, index=True),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False, index=True),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("supersedes_id", sa.String(36), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("approval_status", sa.String(20), nullable=False, server_default="PENDING", index=True),
        sa.Column("approved_by", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.UniqueConstraint("org_id", "storage_key", "version", name="uq_documents_version"),
    )
    op.create_index(
        "ix_documents_current", "documents", ["org_id", "is_current", "doc_type"]
    )
    op.create_index(
        "ix_documents_expiry", "documents", ["org_id", "valid_until"]
    )

    if not _is_postgres():
        # Documented no-op, matching 003-005. SQLite has no row-level security,
        # so on SQLite the tenant boundary is the application layer alone - which
        # is exactly why the RLS tests run against real PostgreSQL.
        return

    for table in ("org_facts", "documents"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        tenant = "org_id = app.current_org()"
        _policy(table, f"{table}_select", "SELECT", tenant)
        _policy(table, f"{table}_insert", "INSERT", "false", check=tenant)
        _policy(table, f"{table}_update", "UPDATE", tenant, check=tenant)
        _policy(table, f"{table}_delete", "DELETE", tenant)

    _grant_runtime()


def _grant_runtime() -> None:
    """Grant the runtime role least-privilege DML on the new tables.

    SELECT, INSERT and UPDATE only - no DELETE, matching migrations 004 and 005.
    A fact history and a document vault are evidence; the application role
    records them, it does not erase them. Superseding a fact is an UPDATE
    (``is_current`` goes false), which is exactly why versioning was chosen over
    deletion: the history has to survive.

    ``sql/grant_runtime_role.sql`` contains a matching ``REVOKE DELETE`` for both
    tables, because its blanket ``ALTER DEFAULT PRIVILEGES ... GRANT ... DELETE``
    would otherwise hand DELETE straight back. A GRANT is additive.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT, INSERT, UPDATE ON org_facts, documents TO granada_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_documents_expiry", table_name="documents")
    op.drop_index("ix_documents_current", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_org_facts_current", table_name="org_facts")
    op.drop_table("org_facts")
