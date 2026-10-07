"""The additive-grant trap: a structural guard and a live idempotence test.

This project has now made the same mistake **nine times**. The mechanism never changes:

    sql/grant_runtime_role.sql line 37:  GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES
    ...then a DO block REVOKES selectively.

So a table that is not named in the DO block **keeps DELETE**, and re-running the script -
the thing operators do, and the thing the script's own comments call "additive" - silently
hands privileges back. The posture looks correct until somebody re-runs it.

It fired again during this phase, while adding the submission tables: the first fix named
`submission_attempts` and `submission_receipts`, and re-running the script showed
`submission_packages` had gone from `D=false` to `D=true`. Caught only because the posture
was verified AFTER applying the script, which is the rule this project wrote down for
exactly this reason.

The static test would have caught it. The live test catches the next one, whatever it is,
without anybody having to remember which tables are special.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

GRANT_SCRIPT = BACKEND / "sql" / "grant_runtime_role.sql"

#: `UPDATE` no, `DELETE` no. A history its own subject can edit is not a history.
#:
#: **Taken from the live, verified posture rather than from reasoning about table names.**
#: The first version of this file guessed, and put `jobs`, `job_attempts`,
#: `decision_records` and `model_invocations` here - all four of which legitimately allow
#: UPDATE, because their state advances. A test that demands the wrong posture is a test
#: that gets deleted, so the classification is now the measured one.
FULLY_APPEND_ONLY: frozenset[str] = frozenset({
    "agent_activity",
    "application_transitions",
    "donor_research",
    "mail_approvals",
    "mail_send_attempts",
    "submission_attempts",
    "submission_receipts",
})

#: `DELETE` no, `UPDATE` yes. The row's LIFECYCLE advances - a job goes QUEUED to RUNNING, a
#: document is superseded by setting `is_current` false, a package advances to AUTHORISED -
#: but its EXISTENCE is the record and must not be erasable.
#:
#: `submission_packages` is the entry that was caught by verifying the posture after
#: re-running the script: it had gone from `D=false` to `D=true`.
DELETE_REVOKED_UPDATE_ALLOWED: frozenset[str] = frozenset({
    "decision_records",
    "documents",
    "job_attempts",
    "jobs",
    "mail_send_intents",
    "model_invocations",
    "org_facts",
    "submission_packages",
})

#: Protected by a DIFFERENT mechanism, and deliberately not in the list above.
#:
#: The runtime role is denied SELECT on `alembic_version` altogether, so there is no
#: privilege to revoke selectively - it is unreachable rather than read-only. Reasoned about
#: as "append-only" at first, which made this guard demand an entry in the grant script that
#: would have granted the role a privilege it is not supposed to have at all.
#:
#: This is also why the readiness check verifies the schema by STRUCTURE: the revision
#: number is not readable by the role that runs the service.
SELECT_DENIED = frozenset({"alembic_version"})

#: Kept as the union so the DELETE-side assertions read the same way as before.
APPEND_ONLY = FULLY_APPEND_ONLY

ALL_DELETE_REVOKED = FULLY_APPEND_ONLY | DELETE_REVOKED_UPDATE_ALLOWED


def _script() -> str:
    return GRANT_SCRIPT.read_text(encoding="utf-8")


def _delete_revoke_array(text: str) -> set[str]:
    """Table names in the DO block's FOREACH array."""
    body = text.split("FOREACH evidence_table IN ARRAY ARRAY[", 1)[1]
    body = body.split("]", 1)[0]
    return set(re.findall(r"'([a-z_]+)'", body))


def _update_revoke_set(text: str) -> set[str]:
    """Table names in the UPDATE-revoke IN list."""
    match = re.search(
        r"IF evidence_table IN\s*\((.*?)\)\s*THEN", text, re.DOTALL
    )
    assert match, "the UPDATE-revoke list is gone"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


# ===========================================================================
# STATIC: THE SCRIPT NAMES EVERY TABLE THAT MUST BE PROTECTED
# ===========================================================================
def test_every_append_only_table_is_revoked_in_the_grant_script():
    """THE guard that would have caught the ninth occurrence before it fired.

    A table absent from this array is not protected at all: line 37 grants DELETE to every
    table in the schema, and only this array takes it away.
    """
    named = _delete_revoke_array(_script())
    missing = sorted(ALL_DELETE_REVOKED - named)
    assert not missing, (
        f"these tables keep DELETE from the broad GRANT on line 37 because the DO block "
        f"does not name them: {missing}. Re-running this script silently re-grants DELETE "
        f"on an append-only table - which has now happened nine times."
    )


def test_every_append_only_table_has_update_revoked():
    """`UPDATE` on a history row is a rewrite, which is worse than a delete: the row still
    looks authoritative."""
    named = _update_revoke_set(_script())
    missing = sorted(FULLY_APPEND_ONLY - named)
    assert not missing, f"these fully append-only tables still allow UPDATE: {missing}"


def test_the_lifecycle_tables_deliberately_keep_update():
    """The counterpart, so the guard cannot be satisfied by revoking UPDATE everywhere.

    Revoking UPDATE on a package or a send intent would freeze its status, so an application
    could never leave DRAFT and an email could never be sent. An over-broad fix here breaks
    the product, and a test that only checked one direction would encourage it.
    """
    named = _update_revoke_set(_script())
    wrongly_revoked = sorted(DELETE_REVOKED_UPDATE_ALLOWED & named)
    assert not wrongly_revoked, (
        f"these tables need UPDATE to advance their status but the script revokes it: "
        f"{wrongly_revoked}"
    )


def test_the_script_still_has_the_broad_grant_that_makes_this_necessary():
    """If line 37 ever changed to a narrower grant, the DO block would become belt and
    braces rather than the only protection - and this whole file's reasoning would need
    revisiting rather than silently becoming wrong."""
    assert "ON ALL TABLES IN SCHEMA public" in _script(), (
        "the broad GRANT is gone; re-examine whether the selective REVOKEs are still the "
        "only thing protecting the append-only tables, and update this file's docstring"
    )


# ===========================================================================
# LIVE: THE SCRIPT IS IDEMPOTENT
# ===========================================================================
#: Read from `pg_class`, not `information_schema.tables`.
#:
#: On this PostgreSQL, calling `has_table_privilege` with a name taken from
#: `information_schema.tables` fails with `relation "role_column_grants" does not exist` -
#: an internal view the information_schema path depends on. `pg_class.oid` avoids it, and
#: `has_table_privilege` has an oid overload precisely for this.
POSTURE_SQL = """
SELECT c.relname || ':' ||
       has_table_privilege('granada_app', c.oid, 'UPDATE')::text || ':' ||
       has_table_privilege('granada_app', c.oid, 'DELETE')::text
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'r'
 ORDER BY c.relname
"""


def _admin_url() -> str | None:
    return os.environ.get("GRANADA_ADMIN_DATABASE_URL") or None


@pytest.fixture
def admin_engine():
    url = _admin_url()
    if not url:
        pytest.skip("GRANADA_ADMIN_DATABASE_URL is not set; the live posture needs the owner")
    from sqlalchemy import create_engine

    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _posture(engine) -> dict[str, str]:
    """`{table: "update:delete"}`.

    The SQL concatenates three fields into ONE column, so indexing `row[1]` raised
    IndexError and the idempotence assertion never ran - a test that fails before it checks
    anything looks like a test that is checking something.
    """
    from sqlalchemy import text

    with engine.connect() as connection:
        rows = connection.execute(text(POSTURE_SQL)).all()
    posture: dict[str, str] = {}
    for row in rows:
        parts = str(row[0]).split(":")
        if len(parts) != 3:  # pragma: no cover - defensive
            continue
        posture[parts[0]] = f"{parts[1]}:{parts[2]}"
    return posture


def test_re_running_the_grant_script_changes_nothing(admin_engine):
    """THE test that catches the next occurrence, whatever table it is.

    Runs the script twice and compares the resulting privileges for every table in the
    schema. A table that is not named in the DO block keeps the broad GRANT's DELETE, so a
    re-run would differ - and this assertion does not need to know in advance which table
    that is.

    This is the check that caught the ninth occurrence, performed manually at the time.
    """
    from sqlalchemy import text

    script = _script()
    # `alembic_version` is denied SELECT to the runtime role, which is a separate posture
    # asserted elsewhere; include it anyway, because a change there would matter too.
    with admin_engine.connect() as connection:
        connection.execute(text(script))
        connection.commit()

    first = _posture(admin_engine)

    with admin_engine.connect() as connection:
        connection.execute(text(script))
        connection.commit()

    second = _posture(admin_engine)

    differing = {
        table: (first.get(table), second.get(table))
        for table in set(first) | set(second)
        if first.get(table) != second.get(table)
    }
    assert not differing, (
        "re-running the grant script changed the posture. The script is ADDITIVE and is "
        f"meant to be idempotent; these tables drifted: {differing}"
    )

    # And the posture is the intended one, not merely stable.
    missing = sorted(ALL_DELETE_REVOKED - set(second))
    assert not missing, (
        f"these protected tables do not exist in the database: {missing} - the guard "
        "cannot verify them, and a skipped guard is not a guard"
    )
    for table in sorted(ALL_DELETE_REVOKED):
        update, delete = second[table].split(":")
        assert delete == "false", f"{table} is deletable through the runtime role"
        if table in FULLY_APPEND_ONLY:
            assert update == "false", f"{table} is rewritable through the runtime role"


def test_the_live_posture_matches_the_model_layer(admin_engine):
    """Every table the models declare should be reachable by the runtime role at all.

    A table with no privileges is a table the service cannot use, and a migration that
    forgot its GRANT fails at the first request rather than at deploy time.
    """
    import models
    from sqlalchemy import text

    declared = set(models.Base.metadata.tables)
    with admin_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT c.relname, has_table_privilege('granada_app', c.oid, 'SELECT') "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
        ).all()
    present = {row[0] for row in rows}

    expected_denied = SELECT_DENIED
    unreachable = sorted(
        table
        for table, can_select in rows
        if table in declared and not can_select and table not in expected_denied
    )
    assert not unreachable, (
        f"the runtime role cannot read these tables the models declare: {unreachable}"
    )


def test_alembic_version_is_unreachable_rather_than_read_only(admin_engine):
    """The ninth table is protected by denial, not by revocation.

    Asserted separately because lumping it in with the append-only tables produced a guard
    that demanded the grant script hand the runtime role a SELECT it must not have. The
    readiness check verifies the schema by structure for exactly this reason.
    """
    from sqlalchemy import text

    with admin_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT has_table_privilege('granada_app', c.oid, 'SELECT') "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'alembic_version'"
            )
        ).first()
    assert row is not None, "alembic_version does not exist"
    assert row[0] is False, (
        "the runtime role can read alembic_version. That is not merely a privilege "
        "question: the readiness check verifies the schema by STRUCTURE precisely because "
        "this is denied, and granting it would make that reasoning wrong."
    )
