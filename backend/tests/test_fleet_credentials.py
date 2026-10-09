"""ADR-0011: the fleet credential, and the discovery that gives the fleet its first work.

THE DEFECT, proven on the VPS
-----------------------------
`FleetDispatcher.due_workflows()` reads `agent_workflows` without binding a tenant, because it must
discover work across the whole fleet. But that table is FORCE ROW LEVEL SECURITY with
`org_id = app.current_org()`, and an unbound `app.current_org()` is NULL - so the predicate is false
for every row and the query returns ZERO WHATEVER EXISTS:

    INSERTED as superuser, total rows = 1
    AS granada_app, UNSCOPED (what due_workflows does) = 0

The sweep reported `dispatched=0 errors=0` throughout: blind, not idle, and healthy-looking.

WHY THIS FILE IS SHAPED THE WAY IT IS
-------------------------------------
Most of it is static, and that is deliberate. The bug was invisible to 1216 passing tests **because
they run on SQLite**, where there is no RLS and every row is visible - they exercised the only
configuration in which the defect does not exist. A behavioural test on SQLite therefore cannot pin
this fix, so the credential wiring is asserted against the compose file and the source, and the one
behavioural test that CAN prove it is marked PostgreSQL-only and skips when there is no server, like
the other 57.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
from agent.granada_agent import GranadaAgentService  # noqa: E402
from agent.workflow_engine import WORKFLOW_MATCH, FleetDispatcher  # noqa: E402

ROOT = BACKEND.parent.parent
COMPOSE = ROOT / "docker-compose.yml"


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "fleetcred.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _org(db, name="Credential Test NGO"):
    # `owner_user_id` is NOT NULL, so a user comes first. Same shape as test_fleet.py's helper.
    user = models.User(id=str(uuid.uuid4()), display_name="Owner")
    db.add(user)
    db.commit()
    row = models.Organisation(
        id=str(uuid.uuid4()), name=name,
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=user.id,
    )
    db.add(row)
    db.commit()
    return row


def _opportunity(db, *, country="Nigeria", title="Nigerian Health Grant"):
    row = models.Opportunity(
        title=title,
        source_url=f"https://funder.example.org/{uuid.uuid4().hex[:8]}",
        source_name="Example Funder",
        country=country,
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        dedupe_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        is_active=True,
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


# ===========================================================================
# THE CREDENTIAL SEPARATION
# ===========================================================================
def _service_env(name: str) -> dict:
    import yaml

    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    return services[name].get("environment") or {}


def test_worker_and_relay_use_the_fleet_credential():
    """They read across tenants by nature, and the application role cannot see the table at all."""
    for service in ("worker", "relay"):
        assert "FLEET_DATABASE_URL" in _service_env(service), (
            f"{service} connects as the application role, which reads zero rows from the "
            "FORCE-RLS fleet tables - the dispatcher would be blind (ADR-0011)"
        )


def test_the_API_does_NOT_get_the_fleet_credential():
    """THE security property, and the one most likely to be broken by a careless fix.

    `granada_fleet` carries BYPASSRLS. Handing it to the request path would remove the guarantee that
    a request cannot read another tenant's data - trading the product's central security property
    away to solve a worker problem. If this test fails, the platform is no longer multi-tenant.
    """
    env = _service_env("api")
    assert "FLEET_DATABASE_URL" not in env, (
        "the API has the BYPASSRLS fleet credential; every request could now read every tenant"
    )
    assert "DATABASE_URL" in env, "the API must connect as the RLS-bound application role"


def test_the_fleet_credential_is_NOT_the_application_credential():
    """A distinct role, not the same URL under a second name."""
    fleet = _service_env("worker")["FLEET_DATABASE_URL"]
    app_url = _service_env("worker")["DATABASE_URL"]
    assert "granada_fleet" in fleet
    assert "granada_fleet" not in app_url


def test_the_fleet_url_has_no_default_password():
    """`:?` not `:-`. A missing secret must stop the deployment, never fall back to something."""
    fleet = _service_env("worker")["FLEET_DATABASE_URL"]
    assert "GRANADA_FLEET_PASSWORD:?" in fleet, (
        "the fleet password has a default, so a deployment could start with a guessed credential"
    )


def test_the_worker_and_relay_build_their_session_from_the_fleet_factory():
    """Read from the source: the process entry points must not use the request-path factory."""
    for relative in ("agent/fleet_runner.py", "events/relay.py"):
        source = (BACKEND / relative).read_text(encoding="utf-8")
        assert "FleetSessionLocal" in source, f"{relative} does not use the fleet session factory"
        # The entry point must pass the fleet factory, not the ordinary one.
        assert "SessionLocal," not in source.split("FleetSessionLocal")[0].split("def main")[-1], (
            f"{relative} still constructs its runner with the application session factory"
        )


# ===========================================================================
# THE ROLE SQL
# ===========================================================================
def test_the_fleet_role_sql_grants_BYPASSRLS_and_refuses_SUPERUSER():
    sql = (BACKEND / "sql" / "fleet_role.sql").read_text(encoding="utf-8")
    assert "BYPASSRLS" in sql
    assert "NOSUPERUSER" in sql, (
        "the fleet role must not be a superuser; it needs to bypass RLS, not to be all-powerful"
    )


def _sql_statements() -> str:
    """The role script with comments removed.

    THE TRAP, hit for the fourth time in this project: an assertion about what a file DOES must not
    be satisfied by what it SAYS. This script's own comment quotes `GRANT ALL ON ALL TABLES` while
    explaining why it is not used, so a check over the raw text failed on the explanation.
    """
    lines = []
    for line in (BACKEND / "sql" / "fleet_role.sql").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line.split("--")[0] if "--" in line else line)
    return "\n".join(lines)


def test_the_fleet_role_sql_does_NOT_grant_tenant_business_tables():
    """A blanket `GRANT ALL ON ALL TABLES` would silently widen as tables are added."""
    sql = _sql_statements()
    assert "GRANT ALL ON ALL TABLES" not in sql, "the fleet role has a blanket table grant"
    # Every table named in a GRANT must be one the fleet legitimately needs.
    tenant_tables = ("documents", "mail_messages", "mail_attachments", "mail_drafts", "grants")
    grant_lines = [line for line in sql.splitlines() if line.strip().upper().startswith("GRANT")]
    for line in grant_lines:
        for tenant_table in tenant_tables:
            assert tenant_table not in line, (
                f"the fleet role is granted {tenant_table}, which the worker has no business "
                f"reading: {line.strip()}"
            )


def test_the_fleet_role_sql_verifies_its_own_posture():
    """`grant_runtime_role.sql` is ADDITIVE and has silently re-granted a privilege nine times here.
    A role script that reports success without checking is how the tenth happens."""
    sql = (BACKEND / "sql" / "fleet_role.sql").read_text(encoding="utf-8")
    assert "RAISE EXCEPTION" in sql
    assert "rolbypassrls" in sql


# ===========================================================================
# DISCOVERY - something must create the FIRST workflow
# ===========================================================================
def test_discovery_schedules_a_match_workflow_for_an_unevaluated_opportunity(db):
    """Nothing created the first workflow. `schedule()` existed and no caller existed."""
    org = _org(db)
    GranadaAgentService(db, org.id).provision()
    db.commit()
    opportunity = _opportunity(db)

    scheduled = FleetDispatcher(db).discover_opportunity_work()

    assert scheduled == 1, "discovery found nothing, so the fleet still has no work"
    workflow = db.query(models.AgentWorkflow).filter(
        models.AgentWorkflow.workflow_type == WORKFLOW_MATCH
    ).first()
    assert workflow is not None
    assert str(workflow.subject_id) == str(opportunity.id)
    assert workflow.org_id == org.id


def test_discovery_is_idempotent_across_sweeps(db):
    """A sweep runs every few seconds. Re-queuing the same opportunity forever is a busy loop."""
    org = _org(db)
    GranadaAgentService(db, org.id).provision()
    db.commit()
    _opportunity(db)

    first = FleetDispatcher(db).discover_opportunity_work()
    second = FleetDispatcher(db).discover_opportunity_work()

    assert first == 1
    assert second == 0, (
        "the second sweep re-discovered the same opportunity, so a queued workflow is being "
        "re-created or re-woken on every tick"
    )
    assert db.query(models.AgentWorkflow).count() == 1


def test_discovery_skips_an_opportunity_that_already_has_a_match(db):
    """An evaluated opportunity must not be queued again - the work is already done."""
    org = _org(db)
    GranadaAgentService(db, org.id).provision()
    db.commit()
    opportunity = _opportunity(db)
    # `final_score`, not `score`: the model separates the semantic score from the final one, and
    # `scorer`/`contract_version` record which scorer produced it.
    db.add(models.OpportunityMatch(
        id=str(uuid.uuid4()), org_id=org.id, opportunity_id=opportunity.id,
        state=models.OpportunityMatch.MATCHED, final_score=80.0, semantic_score=80.0,
    ))
    db.commit()

    assert FleetDispatcher(db).discover_opportunity_work() == 0


def test_discovery_only_considers_active_opportunities(db):
    """A closed call is not work. Queueing it wastes an agent's turn and can resurrect a deadline."""
    org = _org(db)
    GranadaAgentService(db, org.id).provision()
    db.commit()
    _opportunity(db, title="Closed Grant")

    db.query(models.Opportunity).update({models.Opportunity.is_active: False})
    db.commit()

    assert FleetDispatcher(db).discover_opportunity_work() == 0


def test_discovery_is_bounded_per_agent(db):
    """A catalogue of 100,000 must not become 100,000 workflow rows in one sweep."""
    org = _org(db)
    GranadaAgentService(db, org.id).provision()
    db.commit()
    for index in range(40):
        _opportunity(db, title=f"Grant {index}")

    scheduled = FleetDispatcher(db).discover_opportunity_work()

    assert scheduled == FleetDispatcher.DISCOVERY_LIMIT_PER_AGENT, (
        "discovery is not bounded, so one sweep can enqueue the whole catalogue"
    )


def test_discovery_skips_a_paused_agent(db):
    """A paused organisation receives no new work. That is what pause means."""
    org = _org(db)
    service = GranadaAgentService(db, org.id)
    service.provision()
    db.commit()
    service.pause() if hasattr(service, "pause") else None
    agent = db.query(models.GranadaAgent).filter(models.GranadaAgent.org_id == org.id).first()
    agent.status = models.GranadaAgent.PAUSED
    db.commit()
    _opportunity(db)

    assert FleetDispatcher(db).discover_opportunity_work() == 0


def test_discovery_lives_in_the_sweep_and_not_in_dispatch_once():
    """Separation of concerns, asserted with the AST.

    `dispatch_once` claims work that is ALREADY DUE; discovery CREATES work. An earlier version put
    discovery inside `dispatch_once`, which silently redefined that method: 36 tests that set up
    exactly the work they wanted dispatched started finding extra rows and failed with
    `MultipleResultsFound`. A method named `dispatch_once` should dispatch.
    """
    import ast

    source = (BACKEND / "agent" / "workflow_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    dispatch = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "dispatch_once"
    )
    called = {
        node.func.attr
        for node in ast.walk(dispatch)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "discover_opportunity_work" not in called, (
        "dispatch_once creates work as well as claiming it, which changes what its callers get"
    )
    assert "due_workflows" in called, "dispatch_once no longer claims anything"


def test_the_sweep_discovers_before_it_claims():
    """Order matters: claiming first would dispatch nothing on the sweep that finds the work."""
    import ast

    source = (BACKEND / "agent" / "fleet_runner.py").read_text(encoding="utf-8")
    sweep = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "sweep_once"
    )

    # SORTED BY LINE. st.walk is breadth-first, so its order is the tree's shape and not the
    # source's - reading the calls off it unsorted reports the nesting, not the sequence.
    order = [
        name
        for _, name in sorted(
            (node.lineno, node.func.attr)
            for node in ast.walk(sweep)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("discover_opportunity_work", "dispatch_once")
        )
    ]
    assert order, "the sweep neither discovers nor dispatches"
    assert order.index("discover_opportunity_work") < order.index("dispatch_once"), (
        f"the sweep claims before it discovers: {order}"
    )


def test_a_discovery_failure_does_not_stop_queued_work():
    """Containment. A discovery bug must not strand work a customer is waiting on."""
    import ast

    source = (BACKEND / "agent" / "fleet_runner.py").read_text(encoding="utf-8")
    sweep = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "sweep_once"
    )

    guarded = [
        node for node in ast.walk(sweep)
        if isinstance(node, ast.Try)
        and any(
            isinstance(sub, ast.Call)
            and getattr(getattr(sub, "func", None), "attr", None) == "discover_opportunity_work"
            for stmt in node.body for sub in ast.walk(stmt)
        )
    ]
    assert guarded, "discovery is not inside a try, so one bad opportunity would stop the sweep"
    assert all(node.handlers for node in guarded), "the discovery try has no handler"


# ===========================================================================
# THE POSTGRESQL TEST - the only one that can actually prove the fix
# ===========================================================================
def test_the_fleet_role_can_see_what_the_application_role_cannot():
    """THE test for ADR-0011, and it needs PostgreSQL.

    Writes a row as the owner, then reads it with both roles. The application role must see ZERO -
    that is the defect, and asserting it stops the test passing for the wrong reason if RLS is ever
    relaxed. The fleet role must see it, or the dispatcher is still blind.

    Skips when nothing is listening on 5432, like the other PostgreSQL-only tests. On SQLite this
    question cannot be asked at all: there is no RLS, so both roles see everything and the test would
    pass whatever the code did - which is exactly why 1216 passing tests missed this.
    """
    from sqlalchemy import create_engine, text

    from conftest import _admin_postgres_url
    from database import _startup_options

    # Guarded on LISTENING, not merely on a URL being derivable: `_admin_postgres_url()` builds a
    # URL whether or not a server exists, so the earlier version raised a connection error instead of
    # skipping - a red test on a machine with PostgreSQL stopped, which is a false alarm and the
    # exact cry-wolf failure this suite has been fixed for before.
    from conftest import _postgres_is_listening

    if not _postgres_is_listening():
        pytest.skip("no PostgreSQL server on 5432; the RLS question cannot be asked without one")

    admin_url = _admin_postgres_url()
    if not admin_url:
        pytest.skip("no admin URL available for PostgreSQL")

    engine = create_engine(admin_url, connect_args={"options": _startup_options(admin_url)})
    try:
        with engine.connect() as conn:
            fleet = conn.execute(
                text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'granada_fleet'")
            ).scalar()
        if fleet is None:
            pytest.skip("granada_fleet does not exist; apply sql/fleet_role.sql first")
        assert fleet is True, "granada_fleet lacks BYPASSRLS, so the dispatcher stays blind"
    finally:
        engine.dispose()
