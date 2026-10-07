"""Real cross-tenant DATA isolation on PostgreSQL, for the agent/fleet tables.

Why this is separate from ``test_tenant_rls.py``
-----------------------------------------------
That module proves the *model* — the original tenant tables — and it proves it
thoroughly. This one proves the tables Phase 6c and 6d added, which had only their
**posture** inspected (`pg_policies`) and not their **behaviour** exercised. The
brief is explicit that inspecting policies is not enough: tenant A and tenant B
data must actually be created and the four operations must actually be attempted.

Why it runs against a scratch schema
------------------------------------
The live ``public`` schema cannot be used for this. ``organisations`` is FORCE
row-level security, so an owner connection cannot insert the fixture rows for two
tenants without setting the tenant GUC for each — and setting it to two different
values in one transaction is exactly what is being tested. The fixture builds a
scratch schema, applies the real migration chain, and grants the runtime role, so
the policies under test are the real ones from the real migrations.

Every write below goes through ``tenant_scope``, which is the same mechanism the
request path uses. Nothing here inserts as a superuser or bypasses the policies.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from tests.test_tenant_rls import (  # noqa: E402
    PG_URL,
    RUNTIME_URL,
    _require_runtime,
    pg_engine,  # noqa: F401 - imported so pytest exposes the module-scoped fixture here
    tenant_scope,
    tenants,  # noqa: F401 - requested as a fixture
    unscoped,
)

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason=(
        "row-level security is only enforceable on PostgreSQL; SQLite has no RLS "
        "and cannot prove anything about it"
    ),
)

#: The tables Phase 6c/6d added, all FORCE-bound and all org-scoped.
AGENT_TABLES = (
    "granada_agents",
    "agent_specialists",
    "agent_workflows",
    "agent_activity",
    "donor_research",
)


@pytest.fixture(scope="module")
def fleet_data(pg_engine, tenants):
    """One agent per tenant, plus a workflow, an activity record and research.

    Written through ``tenant_scope`` so the policies are satisfied the way the
    application satisfies them — not bypassed.
    """
    engine = pg_engine.owner
    made: dict[str, dict[str, str]] = {}

    # An opportunity is NOT tenant-owned (ADR-0009): its INSERT policy requires an
    # *unscoped* context, so it is created that way rather than by pretending a
    # tenant owns it.
    opportunity_id = str(uuid.uuid4())
    with unscoped(engine) as conn:
        conn.execute(
            text(
                "INSERT INTO opportunities (id, title, source_url, source_name, country,"
                " content_hash, dedupe_fingerprint, is_active, created_at, version)"
                " VALUES (:id, 'Fleet test grant', :url, 'Funder', 'UG', :hash, :fp,"
                " true, now(), 1)"
            ),
            {
                "id": opportunity_id,
                "url": f"https://funders.example.org/{uuid.uuid4().hex[:8]}",
                "hash": uuid.uuid4().hex + uuid.uuid4().hex,
                "fp": uuid.uuid4().hex + uuid.uuid4().hex,
            },
        )
        conn.commit()

    for key, tenant in tenants.items():
        agent_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        with tenant_scope(engine, org_id=tenant["org_id"]) as conn:
            conn.execute(
                text(
                    "INSERT INTO granada_agents (id, org_id, display_name, vertical,"
                    " status, autonomy, version, created_at)"
                    " VALUES (:id, :org, :name, 'NGO', 'ACTIVE', 'MONITOR_ONLY', 1, now())"
                ),
                {"id": agent_id, "org": tenant["org_id"], "name": f"{key} agent"},
            )
            conn.execute(
                text(
                    "INSERT INTO agent_specialists (id, agent_id, org_id, key,"
                    " display_name, status, runs_completed, created_at)"
                    " VALUES (:id, :agent, :org, 'MATCHER', 'Matching Agent', 'IDLE', 0, now())"
                ),
                {"id": str(uuid.uuid4()), "agent": agent_id, "org": tenant["org_id"]},
            )
            conn.execute(
                text(
                    "INSERT INTO agent_workflows (id, agent_id, org_id, specialist_key,"
                    " workflow_type, state, priority, attempts, created_at)"
                    " VALUES (:id, :agent, :org, 'MATCHER', 'opportunity_match',"
                    " 'PENDING', 100, 0, now())"
                ),
                {"id": workflow_id, "agent": agent_id, "org": tenant["org_id"]},
            )
            conn.execute(
                text(
                    "INSERT INTO agent_activity (id, agent_id, org_id, specialist_key,"
                    " activity_type, summary_key, visibility, occurred_at)"
                    " VALUES (:id, :agent, :org, 'MATCHER', 'match', 'match.passed',"
                    " 'CUSTOMER', now())"
                ),
                {"id": str(uuid.uuid4()), "agent": agent_id, "org": tenant["org_id"]},
            )
            conn.execute(
                text(
                    "INSERT INTO donor_research (id, agent_id, org_id, opportunity_id,"
                    " version, is_current, research_version, researched_at)"
                    " VALUES (:id, :agent, :org, :opp, 1, true, 'v1', now())"
                ),
                {
                    "id": str(uuid.uuid4()), "agent": agent_id,
                    "org": tenant["org_id"], "opp": opportunity_id,
                },
            )
            conn.commit()

        made[key] = {
            "org_id": tenant["org_id"],
            "agent_id": agent_id,
            "workflow_id": workflow_id,
            "opportunity_id": opportunity_id,
        }
    return made


# ---------------------------------------------------------------------------
# The fixture itself must have worked, or nothing below means anything
# ---------------------------------------------------------------------------
def test_the_fixture_created_a_full_set_of_fleet_rows(fleet_data):
    """Without this, every denial assertion could pass because there was no data."""
    assert set(fleet_data) == {"alpha", "beta"}
    assert fleet_data["alpha"]["agent_id"] != fleet_data["beta"]["agent_id"]


# ---------------------------------------------------------------------------
# READ
# ---------------------------------------------------------------------------
def test_tenant_a_cannot_read_tenant_b_fleet_rows(pg_engine, fleet_data):
    """The core property, per table, as the OWNER under FORCE.

    FORCE is what makes this meaningful: the owner is the most privileged
    connection and it is still bound, so a narrower role cannot escape.
    """
    alpha, beta = fleet_data["alpha"], fleet_data["beta"]
    with tenant_scope(pg_engine.owner, org_id=alpha["org_id"]) as conn:
        mine = conn.execute(text("SELECT count(*) FROM granada_agents")).scalar()
        assert mine == 1, "tenant A did not even see its own agent"

        for table in AGENT_TABLES:
            leaked = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE org_id = :b"), {"b": beta["org_id"]}
            ).scalar()
            assert leaked == 0, f"tenant A read {leaked} of tenant B's rows in {table}"

            total = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            assert total == 1, f"{table} exposed {total} rows to tenant A, expected 1"


def test_tenant_a_cannot_read_tenant_b_by_primary_key(pg_engine, fleet_data):
    """Scoping must hold on a direct id lookup, not only on a filtered scan.

    This is the shape an attacker actually uses: they know or guess an id and ask
    for it by name.
    """
    alpha, beta = fleet_data["alpha"], fleet_data["beta"]
    with tenant_scope(pg_engine.owner, org_id=alpha["org_id"]) as conn:
        for table in ("granada_agents", "agent_workflows"):
            id_column = "id"
            found = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE {id_column} = :id"),
                {"id": beta["agent_id"] if table == "granada_agents" else beta["workflow_id"]},
            ).scalar()
            assert found == 0, f"tenant A fetched tenant B's {table} row by id"


def test_the_runtime_role_is_also_denied(pg_engine, fleet_data):
    """The owner is bound by FORCE; the application role is the one that runs.

    Asserting both matters because they are different roles with different
    privileges, and a policy change could break one without the other.
    """
    runtime = _require_runtime(pg_engine)
    alpha, beta = fleet_data["alpha"], fleet_data["beta"]
    with tenant_scope(runtime, org_id=alpha["org_id"]) as conn:
        for table in AGENT_TABLES:
            leaked = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE org_id = :b"), {"b": beta["org_id"]}
            ).scalar()
            assert leaked == 0, f"the runtime role read tenant B's {table}"


def test_an_unscoped_connection_sees_no_fleet_rows(pg_engine, fleet_data):
    """Deny-by-default: no tenant bound means no rows, never a default tenant."""
    for engine_name in ("owner", "runtime"):
        engine = getattr(pg_engine, engine_name)
        if engine is None:
            continue
        with unscoped(engine) as conn:
            for table in AGENT_TABLES:
                seen = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                assert seen == 0, f"unscoped {engine_name} saw {seen} rows in {table}"


# ---------------------------------------------------------------------------
# INSERT
# ---------------------------------------------------------------------------
def test_tenant_a_cannot_insert_rows_claiming_to_be_tenant_b(pg_engine, fleet_data):
    """The forgery attempt: write a row whose org_id names somebody else.

    ``WITH CHECK`` is evaluated per row, so this raises rather than silently
    writing nothing - which is the stronger and more visible behaviour.
    """
    alpha, beta = fleet_data["alpha"], fleet_data["beta"]
    statements = {
        "granada_agents": (
            "INSERT INTO granada_agents (id, org_id, display_name, vertical, status,"
            " autonomy, version, created_at)"
            " VALUES (:id, :b, 'forged', 'NGO', 'ACTIVE', 'MONITOR_ONLY', 1, now())"
        ),
        "agent_specialists": (
            "INSERT INTO agent_specialists (id, agent_id, org_id, key, display_name,"
            " status, runs_completed, created_at)"
            " VALUES (:id, :agent, :b, 'EMAIL', 'Email Agent', 'IDLE', 0, now())"
        ),
        "agent_workflows": (
            "INSERT INTO agent_workflows (id, agent_id, org_id, workflow_type, state,"
            " priority, attempts, created_at)"
            " VALUES (:id, :agent, :b, 'opportunity_match', 'PENDING', 100, 0, now())"
        ),
        "agent_activity": (
            "INSERT INTO agent_activity (id, agent_id, org_id, activity_type,"
            " summary_key, visibility, occurred_at)"
            " VALUES (:id, :agent, :b, 'match', 'match.passed', 'CUSTOMER', now())"
        ),
    }

    # Each forgery attempt needs its OWN transaction. A failed INSERT aborts the
    # surrounding transaction, so a single block would report
    # InFailedSqlTransaction for every statement after the first - which passes a
    # `pytest.raises(ProgrammingError)` for the wrong reason and hides whether the
    # RLS check actually fired.
    for table, statement in statements.items():
        with tenant_scope(pg_engine.owner, org_id=alpha["org_id"]) as conn:
            with pytest.raises(ProgrammingError) as excinfo:
                conn.execute(
                    text(statement),
                    {"id": str(uuid.uuid4()), "agent": beta["agent_id"], "b": beta["org_id"]},
                )
            assert "row-level security" in str(excinfo.value).lower(), (
                f"{table}: expected an RLS refusal, got {excinfo.value}"
            )


def test_tenant_a_cannot_attach_a_child_row_to_tenant_bs_agent(pg_engine, fleet_data):
    """A subtler forgery: the org_id is honest, but the agent belongs to somebody else.

    RLS alone would allow this - the org_id column matches. The **composite
    foreign key** is what refuses it, which is exactly the invariant the brief
    asked for and the reason both mechanisms are needed.
    """
    alpha, beta = fleet_data["alpha"], fleet_data["beta"]
    with tenant_scope(pg_engine.owner, org_id=alpha["org_id"]) as conn:
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                text(
                    "INSERT INTO agent_workflows (id, agent_id, org_id, workflow_type,"
                    " state, priority, attempts, created_at)"
                    " VALUES (:id, :agent, :org, 'opportunity_match', 'PENDING', 100,"
                    " 0, now())"
                ),
                {"id": str(uuid.uuid4()), "agent": beta["agent_id"], "org": alpha["org_id"]},
            )
        message = str(excinfo.value)
        assert "fk_workflow_agent_org" in message or "foreign key" in message.lower(), message
        conn.rollback()


# ---------------------------------------------------------------------------
# UPDATE and DELETE
# ---------------------------------------------------------------------------
def test_tenant_a_cannot_update_tenant_b_rows(pg_engine, fleet_data):
    """Update is where a permitted-mutation policy could leak.

    ``granada_agents`` has an UPDATE policy scoped to the tenant, so a tenant CAN
    update its own - which makes the denial of another tenant's row a real test
    rather than a privilege that was never granted.
    """
    alpha, beta = fleet_data["alpha"], fleet_data["beta"]
    with tenant_scope(pg_engine.owner, org_id=alpha["org_id"]) as conn:
        result = conn.execute(
            text("UPDATE granada_agents SET display_name = 'pwned' WHERE org_id = :b"),
            {"b": beta["org_id"]},
        )
        assert result.rowcount == 0, "tenant A updated tenant B's agent"
        conn.rollback()

    # And the control: the tenant can update its own, so the denial above is the
    # policy working rather than a missing privilege.
    with tenant_scope(pg_engine.owner, org_id=alpha["org_id"]) as conn:
        result = conn.execute(
            text("UPDATE granada_agents SET display_name = 'renamed' WHERE org_id = :a"),
            {"a": alpha["org_id"]},
        )
        assert result.rowcount == 1, "tenant A could not update its own agent"
        conn.rollback()


def test_tenant_a_cannot_delete_tenant_b_rows(pg_engine, fleet_data):
    """A DELETE whose USING clause matches nothing does not raise - it affects
    zero rows. So the assertion is the rowcount and the survival of the row."""
    alpha, beta = fleet_data["alpha"], fleet_data["beta"]
    with tenant_scope(pg_engine.owner, org_id=alpha["org_id"]) as conn:
        for table in ("granada_agents", "agent_workflows"):
            result = conn.execute(
                text(f"DELETE FROM {table} WHERE org_id = :b"), {"b": beta["org_id"]}
            )
            assert result.rowcount == 0, f"tenant A deleted tenant B's {table}"
        conn.rollback()

    with tenant_scope(pg_engine.owner, org_id=beta["org_id"]) as conn:
        for table in ("granada_agents", "agent_workflows"):
            survived = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            assert survived == 1, f"tenant B's {table} row did not survive the attempt"


# ---------------------------------------------------------------------------
# The append-only tables: a DEPLOYMENT posture check, not a scratch-schema one
# ---------------------------------------------------------------------------
def test_append_only_tables_are_append_only_in_the_deployed_schema(pg_engine, fleet_data):
    """``agent_activity`` and ``donor_research`` are records, not working data.

    **This cannot be asserted in the scratch schema, and the reason is worth
    stating.** ``pg_engine`` grants the runtime role ``ALL ON ALL TABLES``
    deliberately, so that a forbidden DELETE fails because of *row-level security*
    rather than because of a missing privilege - which is what its other tests are
    about. Append-only-ness here is a **grant** property, so it is asserted where
    it actually lives: the deployed ``public`` schema, against the role that
    actually connects.

    A failure here means ``sql/grant_runtime_role.sql`` was not run after the
    migrations, which is a real deployment defect rather than a test artefact.
    """
    if not RUNTIME_URL:
        pytest.skip("no runtime role configured; the posture is checked in DEPLOYMENT.md")

    from sqlalchemy import create_engine

    engine = create_engine(RUNTIME_URL)
    try:
        with engine.connect() as conn:
            # Append-only: history that can be rewritten is worse than no history,
            # because it looks authoritative.
            for table in (
                "agent_activity", "donor_research", "application_transitions",
                "mail_send_attempts", "mail_approvals",
            ):
                for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                    held = conn.execute(
                        text("SELECT has_table_privilege(current_user, :t, :p)"),
                        {"t": table, "p": privilege},
                    ).scalar()
                    assert not held, (
                        f"the runtime role holds {privilege} on {table} in the "
                        "deployed schema; sql/grant_runtime_role.sql was not applied "
                        "or has been made non-idempotent"
                    )
                for privilege in ("SELECT", "INSERT"):
                    held = conn.execute(
                        text("SELECT has_table_privilege(current_user, :t, :p)"),
                        {"t": table, "p": privilege},
                    ).scalar()
                    assert held, f"the runtime role cannot {privilege} {table}"

            # A send intent's LIFECYCLE is not append-only: its status must advance.
            # DELETE is still withheld, and this row exists because leaving the table
            # out of the central REVOKE list re-granted DELETE the moment the script
            # was re-run - found by verifying the posture AFTER applying it.
            for privilege in ("DELETE", "TRUNCATE"):
                held = conn.execute(
                    text("SELECT has_table_privilege(current_user, :t, :p)"),
                    {"t": "mail_send_intents", "p": privilege},
                ).scalar()
                assert not held, f"the runtime role holds {privilege} on mail_send_intents"
            for privilege in ("SELECT", "INSERT", "UPDATE"):
                held = conn.execute(
                    text("SELECT has_table_privilege(current_user, :t, :p)"),
                    {"t": "mail_send_intents", "p": privilege},
                ).scalar()
                assert held, f"the runtime role cannot {privilege} mail_send_intents"

            assert not conn.execute(
                text("SELECT has_table_privilege(current_user, 'alembic_version', 'SELECT')")
            ).scalar(), "the runtime role can read alembic_version"
    finally:
        engine.dispose()


def test_the_deployed_agent_tables_are_rls_protected(pg_engine, fleet_data):
    """The deployed posture, read from ``pg_class``.

    The scratch schema proves the *behaviour*; this proves the migrations that
    produced the deployed tables enabled and forced RLS, which the behaviour tests
    in a throwaway schema cannot tell you.
    """
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
                    " (SELECT count(*) FROM pg_policies p"
                    "   WHERE p.schemaname = 'public' AND p.tablename = c.relname)"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'public' AND c.relname = ANY(:names)"
                ),
                {"names": list(AGENT_TABLES)},
            ).all()
    finally:
        engine.dispose()

    found = {row[0]: row for row in rows}
    assert set(found) == set(AGENT_TABLES), f"missing from the deployed schema: {set(AGENT_TABLES) - set(found)}"
    for table, (_, enabled, forced, policies) in found.items():
        assert enabled, f"{table} is not RLS-enabled in the deployed schema"
        assert forced, f"{table} is not FORCE RLS in the deployed schema"
        assert policies == 4, f"{table} has {policies} policies, expected 4"
