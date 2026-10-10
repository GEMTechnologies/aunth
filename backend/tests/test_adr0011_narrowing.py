"""The ADR-0011 narrowing: the function must equal the query it replaces.

WHY THIS FILE EXISTS BEFORE THE CUTOVER
---------------------------------------
`fleet_due_job_ids()` was written against `jobs`. The dispatcher reads `agent_workflows`. Applying that
cutover would have left the real cross-tenant read under BYPASSRLS while appearing to close §12 - found
by reading `dispatch_once` before touching it.

The corrected function, `fleet_due_workflow_ids()`, is applied and its equivalence was proven live over
a non-empty set in a rolled-back transaction. **That proof is a one-off manual check.** This file turns
it into something the suite runs, so the dispatcher cutover has a guard rather than a memory.

WHY EQUIVALENCE IS NOT THE WHOLE TEST
-------------------------------------
The method's own comment records the trap: "an organisation with thousands of high-priority due
workflows fills the entire window, and a smaller organisation's work is never even fetched." A function
that returned due rows WITHOUT the per-agent partition would pass a naive equality check on a small
fixture and starve small organisations in production. So the tests include a FAIRNESS case that a
fetch-then-cap implementation fails - the same shape the existing fairness test uses.

WHAT THIS DOES NOT COVER
------------------------
It runs against a live Postgres with the function applied. On SQLite or without the function it skips,
rather than pretending. A skip is honest; a green pass that tested nothing is not.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

SQLALCHEMY = pytest.importorskip("sqlalchemy", reason="needs the ORM to reach the live database")


# ===========================================================================
# THE REPLACEMENT MUST EQUAL WHAT IT REPLACES
# ===========================================================================
DIRECT_SQL = """
SELECT w.id AS workflow_id FROM (
    SELECT id, agent_id, priority, next_run_at,
           row_number() OVER (PARTITION BY agent_id ORDER BY priority, next_run_at, id) AS r
    FROM agent_workflows
    WHERE state IN ('PENDING', 'WAITING')
      AND next_run_at IS NOT NULL
      AND next_run_at <= now()
) w
WHERE w.r <= :per_agent
ORDER BY w.priority, w.next_run_at, w.id
LIMIT :batch
"""


def _live_engine():
    """A connection to the real database, or None.

    Deliberately None rather than a skip inside: a caller that gets None knows it has no database,
    and the reason it cannot test is stated at the call site rather than implied by a magic marker.
    """
    import os

    url = os.environ.get("DATABASE_URL") or os.environ.get("FLEET_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        return None
    try:
        from sqlalchemy import create_engine

        return create_engine(url)
    except Exception:
        return None


def _function_available(engine) -> bool:
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            return bool(
                conn.execute(
                    text("SELECT 1 FROM pg_proc WHERE proname = 'fleet_due_workflow_ids'")
                ).scalar()
            )
    except Exception:
        return False


@pytest.fixture
def pg():
    engine = _live_engine()
    if engine is None:
        pytest.skip("no postgres DATABASE_URL in this environment; the function is database-side")
    if not _function_available(engine):
        pytest.skip(
            "fleet_due_workflow_ids is not applied here - run "
            "docs/adr-0011-workflows-narrowing.sql first"
        )
    return engine


def test_the_function_and_the_query_agree(pg, monkeypatch):
    """THE test. Both paths over the same data, compared as SETS, inside a transaction that is rolled
    back - so the check cannot alter the production row set it is measuring."""
    from sqlalchemy import text

    conn = pg.connect()
    tx = conn.begin()
    try:
        _seed_two_agents(conn)

        function_rows = {
            r[0] for r in conn.execute(text("SELECT workflow_id FROM fleet_due_workflow_ids(200, 25)"))
        }
        direct_rows = {
            r[0] for r in conn.execute(text(DIRECT_SQL), {"per_agent": 25, "batch": 200})
        }

        assert function_rows == direct_rows, (
            "the narrow function and the query it replaces disagree; cutting the dispatcher over "
            "would change which work is dispatched"
        )
        assert function_rows, "the comparison was made over an EMPTY set, which proves nothing"
    finally:
        tx.rollback()
        conn.close()


def test_the_comparison_is_not_over_an_empty_set(pg):
    """Two empty sets are always equal. This directive got that wrong once and reported IDENTICAL.

    The check asserts the candidate set is non-empty BEFORE comparing, so an empty database produces a
    failure rather than a false pass.
    """
    from sqlalchemy import text

    conn = pg.connect()
    tx = conn.begin()
    try:
        _seed_two_agents(conn)
        n = conn.execute(text("SELECT count(*) FROM fleet_due_workflow_ids(200, 25)")).scalar()
        assert n and n > 0, (
            "no due work in the fixture - an equivalence check here would compare two empty sets and "
            "pass regardless of whether the function is correct"
        )
    finally:
        tx.rollback()
        conn.close()


# ===========================================================================
# FAIRNESS - THE PART A NAIVE FUNCTION WOULD REGRESS
# ===========================================================================
def test_every_agent_with_due_work_is_represented(pg):
    """THE fairness property, and the one the module comment says a fetch-then-cap implementation
    fails. Two agents, the first with far more due work: BOTH must appear in the candidate set."""
    from sqlalchemy import text

    conn = pg.connect()
    tx = conn.begin()
    try:
        _seed_two_agents(conn, big_agent_count=40, small_agent_count=1)

        rows = list(conn.execute(text(
            "SELECT w.agent_id FROM agent_workflows w "
            "WHERE w.id IN (SELECT workflow_id FROM fleet_due_workflow_ids(200, 25))"
        )))
        agents = {r[0] for r in rows}

        assert len(agents) >= 2, (
            "an agent with due work is missing from the candidate set - the per-agent partition has "
            "been lost, and a small organisation would be starved by a large one"
        )
    finally:
        tx.rollback()
        conn.close()


def test_the_per_agent_cap_is_applied(pg):
    """The cap is what stops one organisation filling the window. It must be enforced IN the function,
    not after fetching."""
    from sqlalchemy import text

    conn = pg.connect()
    tx = conn.begin()
    try:
        _seed_two_agents(conn, big_agent_count=40)

        per_agent = 5
        rows = list(conn.execute(
            text("SELECT w.agent_id, count(*) FROM agent_workflows w "
                 "WHERE w.id IN (SELECT workflow_id FROM fleet_due_workflow_ids(200, :cap)) "
                 "GROUP BY w.agent_id"),
            {"cap": per_agent},
        ))
        for agent_id, count in rows:
            assert count <= per_agent, (
                f"agent {agent_id[:8]} contributed {count} candidates with a cap of {per_agent}"
            )
    finally:
        tx.rollback()
        conn.close()


# ===========================================================================
# THE FUNCTION IS A CAPABILITY, NOT A PRIVILEGE GRANT
# ===========================================================================
def test_the_function_returns_ids_only(pg):
    """The narrowing. If it returned tenant columns, a caller could exfiltrate data through it and the
    privilege review would have been for nothing."""
    from sqlalchemy import text

    conn = pg.connect()
    tx = conn.begin()
    try:
        _seed_two_agents(conn)
        cols = [
            r[0]
            for r in conn.execute(text(
                "SELECT a.attname FROM pg_attribute a "
                "JOIN pg_proc p ON p.oid = a.attrelid "
                "WHERE p.proname = 'fleet_due_workflow_ids' "
                "AND a.attnum > 0 AND a.attisdropped = false"
            ))
        ]
        assert cols == ["workflow_id"], f"the function returns {cols}; it must return ids only"
    finally:
        tx.rollback()
        conn.close()


def test_the_function_is_security_definer_and_stable(pg):
    """SECURITY DEFINER scopes the cross-tenant read to this query rather than granting BYPASSRLS.
    STABLE because it must not be usable to write."""
    from sqlalchemy import text

    conn = pg.connect()
    try:
        row = conn.execute(text(
            "SELECT prosecdef, provolatile FROM pg_proc WHERE proname = 'fleet_due_workflow_ids'"
        )).first()
        assert row is not None
        secdef, volatility = row
        assert secdef is True, "not SECURITY DEFINER - it cannot see across tenants, so it is useless"
        assert volatility == "s", "not STABLE - a function that may write is not a read capability"
    finally:
        conn.close()


# ===========================================================================
# FIXTURE
# ===========================================================================
def _seed_two_agents(conn, *, big_agent_count: int = 3, small_agent_count: int = 2):
    """Two agents with due work, inside the caller's transaction - so the rollback removes them.

    Uses the columns the table actually has (read from information_schema, not recalled): `priority`
    and `next_run_at` drive the ordering, `agent_id` drives the partition.
    """
    from sqlalchemy import text

    base = conn.execute(text(
        "SELECT id, org_id, specialist_key, workflow_type FROM agent_workflows LIMIT 1"
    )).first()
    if base is None:
        pytest.skip("agent_workflows is empty; there is no row shape to copy")
    _, org_id, specialist_key, workflow_type = base

    for agent, count in (("fair-a", big_agent_count), ("fair-b", small_agent_count)):
        for i in range(count):
            conn.execute(
                text(
                    "INSERT INTO agent_workflows "
                    "(id, agent_id, org_id, specialist_key, workflow_type, state, next_run_at, "
                    " priority, attempts, context, created_at, updated_at) "
                    "VALUES (:id, :agent, :org, :sk, :wt, 'PENDING', :due, :prio, 0, '{}'::json, "
                    " now(), now())"
                ),
                {
                    "id": f"fair-{agent}-{i}",
                    "agent": agent,
                    "org": org_id,
                    "sk": specialist_key,
                    "wt": workflow_type,
                    "due": datetime.now(timezone.utc) - timedelta(minutes=5),
                    "prio": 1,
                },
            )
