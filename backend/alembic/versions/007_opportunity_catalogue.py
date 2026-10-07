"""opportunity catalogue: the ingestion contract's successor tables.

Revision ID: 007_opportunity_catalogue
Revises: 006_organisation_intelligence
Create Date: 2026-10-07

Purpose
-------
Phase 4. The existing bot subsystem is the only surviving artifact of Granada's
ingestion pipeline: its schema exists in ``granada_db``, its producer code does
not exist anywhere on this machine, and every one of its tables is empty. Phase 4
is therefore **adapter** work against a documented contract
(``docs/BOT_INGESTION_CONTRACT.md``), not greenfield design.

Four tables:

``opportunities``           canonical, preserving every legacy column and BOTH unique keys
``opportunity_payloads``    raw payload retention, which the legacy table lacked
``opportunity_changes``     change detection, with materiality
``ingestion_jobs``          run outcomes, preserving found-vs-saved

Compatibility, stated precisely
-------------------------------
The two legacy UNIQUE constraints are reproduced and **must not be dropped**:

* ``content_hash`` - SHA-256 width, the content-dedupe key.
* ``source_url`` - the same opportunity re-listed is an update, not an insert.

``content_hash`` is treated as **OPAQUE**. The normalisation that produced it is
not recorded in the repository and is not recoverable from an empty table, so an
adapter that recomputed it would silently duplicate every opportunity whose
producer normalised differently. It is stored as delivered and never derived.

Row-level security: this is the one place the posture INVERTS
------------------------------------------------------------
Every other table in this schema is tenant-owned: ``org_id = app.current_org()``.
The funding catalogue is not. A funding opportunity published on a website
belongs to nobody, and the legacy table had no tenant either. Inventing one would
mean every tenant re-scraped the world, and it would make the shared catalogue
invisible to the tenants who need it.

So the policies are inverted deliberately:

* SELECT is unconditional. Every tenant may read the catalogue.
* INSERT and UPDATE require ``app.current_org() IS NULL`` - that is, an
  **unscoped** context. A tenant-scoped request always has a tenant bound, so a
  tenant *cannot write to the shared catalogue*. That is the security property
  that matters here: not "can they read it" (they should) but "can one tenant
  poison what every other tenant matches against" (they cannot).
* DELETE is never permitted, by anyone.

This reuses the existing session helper rather than inventing a second one:
``app.current_org()`` already returns NULL when no tenant is bound. The
consequence is recorded as ADR-0009, because it is a deliberate exception to
"tenant unknown is a deny" - here, tenant unknown is the *ingestion* context and
is the only context permitted to write.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "007_opportunity_catalogue"
down_revision = "006_organisation_intelligence"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(table: str, name: str, command: str, using: str, check: str | None = None) -> None:
    """Create one policy, emitting only the clauses PostgreSQL accepts.

    Mirrors the helper in 003-006. ``USING`` is rejected on INSERT and
    ``WITH CHECK`` on SELECT/DELETE, so the matrix is enforced in one place.
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


#: Read by every tenant; written only when no tenant is bound.
_SHARED_CATALOGUE = "true"
_SYSTEM_ONLY = "app.current_org() IS NULL"


def upgrade() -> None:
    op.create_table(
        "opportunities",
        sa.Column("id", sa.String(36), primary_key=True),
        # --- legacy donor_opportunities columns, preserved verbatim ---
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("amount_min", sa.Integer(), nullable=True),
        sa.Column("amount_max", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(10), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_name", sa.String(200), nullable=False, index=True),
        sa.Column("country", sa.String(100), nullable=False, index=True),
        sa.Column("sector", sa.String(100), nullable=True, index=True),
        sa.Column("eligibility_criteria", sa.Text(), nullable=True),
        sa.Column("application_process", sa.Text(), nullable=True),
        sa.Column("contact_email", sa.String(200), nullable=True),
        sa.Column("contact_phone", sa.String(50), nullable=True),
        sa.Column("keywords", sa.JSON(), nullable=True),
        sa.Column("focus_areas", sa.JSON(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False, index=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("last_verified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false(), index=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("verification_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        # --- agentic additions ---
        sa.Column("dedupe_fingerprint", sa.String(64), nullable=False, index=True),
        sa.Column("source_id", sa.String(36), nullable=True, index=True),
        sa.Column("contract_version", sa.String(20), nullable=False, server_default="v1", index=True),
        # The two legacy unique keys. Dropping either would let the pipeline
        # create duplicates the old system did not.
        sa.UniqueConstraint("content_hash", name="uq_opportunities_content_hash"),
        sa.UniqueConstraint("source_url", name="uq_opportunities_source_url"),
        sa.UniqueConstraint("dedupe_fingerprint", name="uq_opportunities_fingerprint"),
    )
    # The legacy table's query-shaped indexes, preserved.
    op.create_index(
        "ix_opportunities_active_country_deadline",
        "opportunities", ["is_active", "country", "deadline"],
    )
    op.create_index(
        "ix_opportunities_active_sector_deadline",
        "opportunities", ["is_active", "sector", "deadline"],
    )
    op.create_index(
        "ix_opportunities_source_scraped", "opportunities", ["source_name", "scraped_at"]
    )

    op.create_table(
        "opportunity_payloads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("opportunity_id", sa.String(36), sa.ForeignKey("opportunities.id"), nullable=True, index=True),
        sa.Column("source_url", sa.Text(), nullable=False, index=True),
        sa.Column("source_name", sa.String(200), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=True, index=True),
        # NOT NULL: "never lose the original payload" is the requirement, so a
        # row that lost it must not be storable.
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False, index=True),
        sa.Column("content_hash", sa.String(64), nullable=True, index=True),
        sa.Column("contract_version", sa.String(20), nullable=False, server_default="v1"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_index(
        "ix_opportunity_payloads_url_received",
        "opportunity_payloads", ["source_url", "received_at"],
    )

    op.create_table(
        "opportunity_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("opportunity_id", sa.String(36), sa.ForeignKey("opportunities.id"), nullable=False, index=True),
        sa.Column("field", sa.String(80), nullable=False, index=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        # No index=True here: the composite below leads with this column, so a
        # separate single-column index would be redundant AND would collide with
        # the composite's name.
        sa.Column("material", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_index(
        "ix_opportunity_changes_material", "opportunity_changes", ["material", "detected_at"]
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), nullable=True, index=True),
        sa.Column("source_name", sa.String(200), nullable=False, index=True),
        sa.Column("country", sa.String(100), nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="RUNNING", index=True),
        sa.Column("opportunities_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opportunities_saved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opportunities_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opportunities_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("contract_version", sa.String(20), nullable=False, server_default="v1"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )

    if not _is_postgres():
        return

    for table in ("opportunities", "opportunity_payloads", "opportunity_changes", "ingestion_jobs"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        # FORCE on the catalogue and its evidence: even the owner is bound, so
        # "the service role can write the catalogue" is a policy decision rather
        # than a property of which role happens to connect.
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')

    # -- shared catalogue ---------------------------------------------------
    _policy("opportunities", "opportunities_select", "SELECT", _SHARED_CATALOGUE)
    _policy("opportunities", "opportunities_insert", "INSERT", "false", check=_SYSTEM_ONLY)
    _policy("opportunities", "opportunities_update", "UPDATE", _SYSTEM_ONLY, check=_SYSTEM_ONLY)
    _policy("opportunities", "opportunities_delete", "DELETE", "false")

    # -- raw payloads: evidence, so system-only writes and never a delete -----
    _policy("opportunity_payloads", "payloads_select", "SELECT", _SHARED_CATALOGUE)
    _policy("opportunity_payloads", "payloads_insert", "INSERT", "false", check=_SYSTEM_ONLY)
    _policy("opportunity_payloads", "payloads_update", "UPDATE", _SYSTEM_ONLY, check=_SYSTEM_ONLY)
    _policy("opportunity_payloads", "payloads_delete", "DELETE", "false")

    # -- change log: tenants may read it; only ingestion writes it -----------
    _policy("opportunity_changes", "changes_select", "SELECT", _SHARED_CATALOGUE)
    _policy("opportunity_changes", "changes_insert", "INSERT", "false", check=_SYSTEM_ONLY)
    _policy("opportunity_changes", "changes_update", "UPDATE", _SYSTEM_ONLY, check=_SYSTEM_ONLY)
    _policy("opportunity_changes", "changes_delete", "DELETE", "false")

    # -- run outcomes: operational, system-only writes -----------------------
    _policy("ingestion_jobs", "ingestion_select", "SELECT", _SHARED_CATALOGUE)
    _policy("ingestion_jobs", "ingestion_insert", "INSERT", "false", check=_SYSTEM_ONLY)
    _policy("ingestion_jobs", "ingestion_update", "UPDATE", _SYSTEM_ONLY, check=_SYSTEM_ONLY)
    _policy("ingestion_jobs", "ingestion_delete", "DELETE", "false")

    _grant_runtime()


def _grant_runtime() -> None:
    """Grant the runtime role DML on the new tables.

    The catalogue tables do get DELETE at the grant layer, unlike the ledger and
    the vault, and the reason is honest rather than tidy: RLS already makes
    ``opportunities_delete`` unconditionally false for every role, so a DELETE
    grant cannot be exercised. Retention and purge of a public web-derived
    catalogue is a legitimate administrative operation, and withholding the
    privilege would not add a control that the policy does not already impose -
    it would only make the posture look stricter than it is.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON opportunities, opportunity_payloads, opportunity_changes, ingestion_jobs
                    TO granada_app;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_table("ingestion_jobs")
    op.drop_index("ix_opportunity_changes_material", table_name="opportunity_changes")
    op.drop_table("opportunity_changes")
    op.drop_index("ix_opportunity_payloads_url_received", table_name="opportunity_payloads")
    op.drop_table("opportunity_payloads")
    op.drop_index("ix_opportunities_source_scraped", table_name="opportunities")
    op.drop_index("ix_opportunities_active_sector_deadline", table_name="opportunities")
    op.drop_index("ix_opportunities_active_country_deadline", table_name="opportunities")
    op.drop_table("opportunities")
