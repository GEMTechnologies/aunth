"""The agent/organisation invariant, proven against real PostgreSQL.

The safeguard the brief asked for before the fleet runs real work: a durable row
must never be able to say *organisation A* while naming *agent B's agent*.

Why this is a separate module with its own scratch schema
---------------------------------------------------------
The invariant is a **composite foreign key**, and testing it against the real
``public`` schema is impossible with a plain owner connection: ``organisations``
is FORCE row-level security, so setup rows are refused before the constraint is
ever reached. The first attempt at this test failed for exactly that reason - RLS
rejected the fixtures, so the mismatch was never inserted and nothing was proven.

So the constraint is exercised in isolation: a throwaway schema with the two
tables and the composite key, and nothing else. That is the right shape for a test
about a *constraint*, because a failure here can only mean the constraint, not a
policy or a privilege.

SQLite has no composite foreign keys, so ``test_fleet.py`` cannot cover this and
says so; this module is where the claim is actually verified.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from tests.test_tenant_rls import PG_URL  # noqa: E402

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="composite foreign keys are only enforceable on PostgreSQL; SQLite cannot test this",
)


@pytest.fixture(scope="module")
def schema():
    """A throwaway schema holding only the constraint under test."""
    name = f"agent_org_{uuid.uuid4().hex[:10]}"
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{name}"'))

            # Mirrors migration 012's shape exactly, and nothing more.
            conn.execute(text(f'''
                CREATE TABLE "{name}".granada_agents (
                    id text PRIMARY KEY,
                    org_id text NOT NULL,
                    display_name text NOT NULL,
                    CONSTRAINT uq_agent_org UNIQUE (org_id),
                    CONSTRAINT uq_agent_id_org UNIQUE (id, org_id)
                )
            '''))
            conn.execute(text(f'''
                CREATE TABLE "{name}".jobs (
                    id text PRIMARY KEY,
                    org_id text,
                    agent_id text,
                    job_type text NOT NULL,
                    CONSTRAINT fk_jobs_agent_org
                        FOREIGN KEY (agent_id, org_id)
                        REFERENCES "{name}".granada_agents (id, org_id)
                )
            '''))
            # Two organisations, one agent each.
            conn.execute(text(f'''
                INSERT INTO "{name}".granada_agents (id, org_id, display_name) VALUES
                  ('agent-a', 'org-a', 'A Agent'),
                  ('agent-b', 'org-b', 'B Agent')
            '''))
        yield name
    finally:
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        finally:
            admin.dispose()


def _insert(engine_conn, schema: str, job_id: str, org_id, agent_id) -> None:
    engine_conn.execute(
        text(
            f'INSERT INTO "{schema}".jobs (id, org_id, agent_id, job_type) '
            "VALUES (:id, :org, :agent, 'opportunity_match')"
        ),
        {"id": job_id, "org": org_id, "agent": agent_id},
    )


def test_a_mismatched_agent_and_organisation_is_rejected(schema):
    """Organisation A with agent B's agent. The database must refuse it.

    This is the exact row shape the brief named, and without the composite key it
    would persist happily - the worker would then act for one organisation while
    carrying another's authority ceiling.
    """
    engine = create_engine(PG_URL)
    try:
        with engine.connect() as conn:
            with pytest.raises(IntegrityError) as excinfo:
                _insert(conn, schema, "mismatch", "org-a", "agent-b")
            assert "fk_jobs_agent_org" in str(excinfo.value)
    finally:
        engine.dispose()


def test_the_matching_pair_is_accepted(schema):
    """The control. Without it, the test above would be satisfied by a constraint
    that refuses everything."""
    engine = create_engine(PG_URL)
    try:
        with engine.connect() as conn:
            _insert(conn, schema, "control", "org-a", "agent-a")
            count = conn.execute(
                text(f'SELECT count(*) FROM "{schema}".jobs WHERE id = :id'),
                {"id": "control"},
            ).scalar()
            assert count == 1
            conn.commit()
    finally:
        engine.dispose()


def test_a_system_job_with_no_agent_is_accepted(schema):
    """MATCH SIMPLE semantics: if any key column is NULL, the check is skipped.

    This is why ``jobs.agent_id`` stays nullable. The outbox relay and an
    uncorrelated webhook legitimately belong to no agent, and forcing a non-null
    agent would attribute system work to a customer who did not ask for it.
    """
    engine = create_engine(PG_URL)
    try:
        with engine.connect() as conn:
            _insert(conn, schema, "system-job", "org-a", None)
            _insert(conn, schema, "unattributed", None, None)
            count = conn.execute(
                text(f'SELECT count(*) FROM "{schema}".jobs WHERE agent_id IS NULL')
            ).scalar()
            assert count == 2
            conn.commit()
    finally:
        engine.dispose()


def test_an_agent_that_does_not_exist_is_rejected(schema):
    """The key also proves the agent is real, not merely consistently named."""
    engine = create_engine(PG_URL)
    try:
        with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                _insert(conn, schema, "ghost", "org-a", "agent-does-not-exist")
    finally:
        engine.dispose()


def test_the_pair_cannot_be_forged_by_creating_a_duplicate_agent_id(schema):
    """Uniqueness on (id, org_id) is what makes the pair referenceable.

    If two agents could share an id across organisations, the composite key would
    still be satisfiable while pointing at the wrong one.
    """
    engine = create_engine(PG_URL)
    try:
        with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(text(
                    f'INSERT INTO "{schema}".granada_agents (id, org_id, display_name) '
                    "VALUES ('agent-a', 'org-b', 'Impostor')"
                ))
    finally:
        engine.dispose()
