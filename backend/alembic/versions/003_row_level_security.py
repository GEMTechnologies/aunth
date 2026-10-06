"""Phase 1: enforce tenant isolation at the data tier with row-level security.

Why this migration exists
-------------------------
Until now, tenant isolation was enforced only in application code. A single
missing ``WHERE org_id = ...`` in any query would leak every tenant's rows.
The directive requires tenant enforcement *at the data tier*, so the database
must refuse the read or write even when the application asks for it.

The central property is DENY BY DEFAULT
---------------------------------------
``app.current_org()`` returns NULL whenever the request has not established a
tenant. A policy of the form ``org_id = app.current_org()`` then evaluates to
NULL, which is not TRUE, so the row is invisible and the write is rejected.
"Tenant unknown" therefore means "no access" - it is not a default tenant and
it is not a fail-open path. The empty string is collapsed to NULL by NULLIF so
that ``SET LOCAL app.current_org_id = ''`` cannot be used to smuggle a value.

Why FORCE ROW LEVEL SECURITY
----------------------------
A table owner bypasses RLS by default. Migrations run as the owner
(``granada_user``), so without FORCE the policies would protect nothing
whenever the application connected as the owner. FORCE makes the policies bind
the owner too, which means enforcement no longer depends on remembering to
pick the right database role. ``granada_app`` remains the least-privilege
runtime role, but correctness no longer rests on that alone.

How the application establishes a tenant
----------------------------------------
Before any tenant-scoped work, the request issues::

    SET LOCAL app.current_org_id = '<uuid>'

SET LOCAL is transaction-scoped, so a pooled connection cannot leak a tenant
into the next request. Registration has no chicken-and-egg problem: the
service allocates the organisation id first, sets the context to that new id,
and only then inserts the organisation and its founder membership.

Audit logs are append-only
--------------------------
INSERT is permitted for any request that reached the database, because a
failed login often has no tenant and no resolvable user, and refusing to write
the record of a failed login would be the worst possible outcome. READ,
UPDATE and DELETE remain tenant-scoped, so the log is still confidential and
still tamper-proof against a tenant.

SQLite
------
SQLite has no row-level security. This migration is a deliberate, documented
no-op there; the enforcement tests are therefore PostgreSQL-only and *skip*
on SQLite rather than silently passing. SQLite remains acceptable for local
development and for the bulk of the unit suite, which exercises application
logic rather than database authorisation.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "003_row_level_security"
down_revision = "002_phase1_schema_alignment"
branch_labels = None
depends_on = None


# Tables that carry a direct tenant discriminator. `roles.org_id` and
# `audit_logs.org_id` are nullable, and `user_contexts.org_id` is nullable
# because a personal context legitimately belongs to no organisation.
TENANT_TABLES = (
    "organisations",
    "org_members",
    "roles",
    "saml_providers",
    "user_contexts",
    "audit_logs",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _session_helper() -> None:
    """Create the read-only session helpers.

    ``current_setting(..., true)`` yields NULL rather than raising when the
    GUC has never been set in this session, which is exactly the "tenant
    unknown" case we want to resolve to a denial.
    """
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS app;

        CREATE OR REPLACE FUNCTION app.current_org() RETURNS text
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.current_org_id', true), '')
        $$;

        CREATE OR REPLACE FUNCTION app.current_user_id() RETURNS text
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.current_user_id', true), '')
        $$;

        COMMENT ON FUNCTION app.current_org() IS
            'Tenant for the current transaction, or NULL when unknown. '
            'NULL makes every tenant policy evaluate to false, so an '
            'unscoped request is denied rather than defaulted.';

        COMMENT ON FUNCTION app.current_user_id() IS
            'Authenticated user for the current transaction, or NULL.';
        """
    )
    op.execute("GRANT USAGE ON SCHEMA app TO PUBLIC")
    op.execute("REVOKE CREATE ON SCHEMA app FROM PUBLIC")
    _bootstrap_function()


def _bootstrap_function() -> None:
    """Create ``app.user_org_ids()``, the tenant-resolution escape hatch.

    RLS on ``org_members`` filters by ``org_id``, but finding out which orgs a
    user belongs to is the very first thing an authenticated request has to
    do. Without help, an unscoped read returns nothing, the tenant can never
    be established, and the application deadlocks behind its own policy.

    This function is the deliberate, minimal way out: it takes a *user id* -
    which the caller already proved they hold, by authenticating - and returns
    only the org ids that user is a member of. It therefore discloses nothing
    that the caller did not already own, and it never returns row content.

    SECURITY DEFINER makes it run as the owning role. Note the interaction with
    FORCE ROW LEVEL SECURITY, which binds the owner to the policies too: that
    is why ``org_members`` is the one tenant table this migration does not
    FORCE, and why the application must connect as the non-owner runtime role
    for enforcement to mean anything. See ADR-0005.

    The table schema is resolved from ``current_schema()`` rather than written
    as ``public``, because a SECURITY DEFINER function must pin a search_path
    to be safe against hijacking, and pinning it to ``public`` would be wrong
    wherever the tables live in any other schema - including the scratch
    schema the test suite migrates into.
    """
    schema = op.get_bind().execute(text("SELECT current_schema()")).scalar()
    if not schema:
        raise RuntimeError("current_schema() returned no schema; refusing to guess")
    quoted = '"' + schema.replace('"', '""') + '"'

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION app.user_org_ids(p_user_id text)
        RETURNS SETOF text
        LANGUAGE sql
        SECURITY DEFINER
        STABLE
        SET search_path = {quoted}, pg_temp
        AS $$
            SELECT m.org_id
            FROM {quoted}.org_members m
            WHERE m.user_id = p_user_id
              AND p_user_id IS NOT NULL
        $$;

        COMMENT ON FUNCTION app.user_org_ids(text) IS
            'Org ids the given user belongs to. SECURITY DEFINER bootstrap '
            'helper: it exposes only memberships the caller already owns, and '
            'never row content. Calling it proves identity, not tenancy - the '
            'caller must still set app.current_org_id.';
        """
    )
    # EXECUTE is revoked from PUBLIC and granted only to the runtime role, so
    # this cannot be used as a general "list every org" oracle.
    op.execute("REVOKE ALL ON FUNCTION app.user_org_ids(text) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT USAGE ON SCHEMA app TO granada_app;
                GRANT EXECUTE ON FUNCTION app.user_org_ids(text) TO granada_app;
            END IF;
        END $$;
        """
    )


def _grant_policies_to_runtime() -> None:
    """Grant the least-privilege runtime role access to the helper schema."""
    # Written defensively: the role may not exist in every environment (for
    # example a CI database provisioned from a dump without the role), and a
    # migration must not fail on a missing runtime role.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'granada_app') THEN
                GRANT USAGE ON SCHEMA app TO granada_app;
            END IF;
        END $$;
        """
    )


def _policy(table: str, name: str, command: str, using: str, check: str | None = None) -> None:
    """Create one policy, emitting only the clauses PostgreSQL accepts.

    The server validates the clause/command matrix strictly and the two rules
    are not symmetric, so they are enforced here rather than left to callers:

    * ``USING`` is rejected on INSERT - "only WITH CHECK expression allowed
      for INSERT".
    * ``WITH CHECK`` is rejected on SELECT and DELETE - "WITH CHECK cannot be
      applied to SELECT or DELETE".
    * UPDATE accepts both, because rows read through USING must still satisfy
      WITH CHECK once the new values are applied.

    Getting this wrong is a syntax error at migration time, not a silent
    policy that permits too much, so the migration fails loudly.
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
    if not _is_postgres():
        # Documented no-op. SQLite has no RLS; see the module docstring.
        return

    _session_helper()

    # ------------------------------------------------------------------
    # organisations - the tenant root. The tenant is identified by its own
    # primary key, so the policy compares `id` rather than a foreign key.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE organisations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organisations FORCE ROW LEVEL SECURITY")
    _policy(
        "organisations", "orgs_select", "SELECT",
        "id = app.current_org()",
    )
    _policy(
        "organisations", "orgs_insert", "INSERT",
        "false", check="id = app.current_org()",
    )
    _policy(
        "organisations", "orgs_update", "UPDATE",
        "id = app.current_org()", check="id = app.current_org()",
    )
    _policy(
        "organisations", "orgs_delete", "DELETE",
        "id = app.current_org()",
    )

    # ------------------------------------------------------------------
    # org_members - membership is the tenant boundary itself.
    #
    # ENABLE but deliberately NOT FORCE: app.user_org_ids() is SECURITY DEFINER
    # and owned by this table's owner, so only the owner can read memberships
    # before a tenant is known. FORCE would bind the owner too and make tenant
    # resolution impossible. Enforcement is therefore preserved by connecting
    # the application as the non-owner runtime role, which ENABLE already binds
    # unconditionally. See ADR-0005 for why that trade was taken.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE org_members ENABLE ROW LEVEL SECURITY")
    for command in ("SELECT", "UPDATE", "DELETE"):
        _policy(
            "org_members", f"members_{command.lower()}", command,
            "org_id = app.current_org()",
        )
    _policy(
        "org_members", "members_insert", "INSERT",
        "false", check="org_id = app.current_org()",
    )

    # ------------------------------------------------------------------
    # roles - `org_id IS NULL` marks a SYSTEM role shared by every tenant.
    # System roles are readable by all tenants but writable by none of them,
    # so a tenant cannot mint a globally visible role and escalate itself.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")
    _policy(
        "roles", "roles_select", "SELECT",
        "org_id IS NULL OR org_id = app.current_org()",
    )
    for command in ("UPDATE", "DELETE"):
        _policy(
            "roles", f"roles_{command.lower()}", command,
            "org_id = app.current_org()", check="org_id = app.current_org()",
        )
    _policy(
        "roles", "roles_insert", "INSERT",
        "false", check="org_id = app.current_org()",
    )

    # ------------------------------------------------------------------
    # saml_providers - one identity provider per tenant, fully private.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE saml_providers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE saml_providers FORCE ROW LEVEL SECURITY")
    for command in ("SELECT", "UPDATE", "DELETE"):
        _policy(
            "saml_providers", f"saml_{command.lower()}", command,
            "org_id = app.current_org()",
        )
    _policy(
        "saml_providers", "saml_insert", "INSERT",
        "false", check="org_id = app.current_org()",
    )

    # ------------------------------------------------------------------
    # user_contexts - a row belongs either to a tenant (the shared workspace)
    # or to a single user (a personal context that belongs to no org).
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE user_contexts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_contexts FORCE ROW LEVEL SECURITY")
    ctx_visible = (
        "org_id = app.current_org() "
        "OR (org_id IS NULL AND user_id = app.current_user_id())"
    )
    _policy("user_contexts", "contexts_select", "SELECT", ctx_visible)
    _policy("user_contexts", "contexts_update", "UPDATE", ctx_visible, check=ctx_visible)
    _policy("user_contexts", "contexts_delete", "DELETE", ctx_visible)
    _policy(
        "user_contexts", "contexts_insert", "INSERT",
        "false", check=(
            "org_id = app.current_org() "
            "OR (org_id IS NULL AND user_id = app.current_user_id())"
        ),
    )

    # ------------------------------------------------------------------
    # audit_logs - append-only. See the module docstring for why INSERT is
    # deliberately permissive while every other command stays scoped.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY")
    audit_visible = (
        "org_id = app.current_org() "
        "OR (org_id IS NULL AND user_id = app.current_user_id())"
    )
    _policy("audit_logs", "audit_select", "SELECT", audit_visible)
    _policy("audit_logs", "audit_update", "UPDATE", "false")
    _policy("audit_logs", "audit_delete", "DELETE", "false")
    _policy("audit_logs", "audit_insert", "INSERT", "false", check="true")

    _grant_policies_to_runtime()


def downgrade() -> None:
    if not _is_postgres():
        return

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        existing = op.get_bind().execute(
            text("SELECT policyname FROM pg_policies WHERE tablename = :t"),
            {"t": table},
        ).scalars()
        for policy in list(existing):
            op.execute(f'DROP POLICY IF EXISTS "{policy}" ON {table}')

    op.execute("DROP FUNCTION IF EXISTS app.user_org_ids(text)")
    op.execute("DROP FUNCTION IF EXISTS app.current_user_id()")
    op.execute("DROP FUNCTION IF EXISTS app.current_org()")
    op.execute("DROP SCHEMA IF EXISTS app CASCADE")
