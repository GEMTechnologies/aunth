"""The database layer's defaults are all "no limit", and no limit is not a neutral choice.

THE DEFECT
----------
`database.py` gave SQLite a 20-second lock bound and gave **PostgreSQL nothing**:

    engine_kwargs = {"pool_pre_ping": True, "echo": settings.database_echo}

Four unbounded waits followed, and the first is a trap that pre-ping makes worse:

| Missing | Default | What it costs |
|---|---|---|
| `connect_timeout` | none — libpq waits for the OS TCP timeout | a partitioned network stalls **every request**, because `pool_pre_ping` issues `SELECT 1` on every checkout. The feature added for resilience becomes the source of the stall |
| `statement_timeout` | `0` — unlimited | one bad plan holds a connection *and* its pool slot forever. The default pool is 5 + 10 overflow: sixteen slow queries and the service serves nothing |
| `idle_in_transaction_session_timeout` | `0` — disabled | a connection leaked inside a transaction holds its locks **forever**, and every writer behind it waits |
| `pool_recycle` | `-1` — never | a connection killed by a NAT idle timer is handed out again and fails on use |

None of these is an error anywhere. They are defaults, and every one is wrong for a service.

VERIFIED AGAINST THE SERVER, NOT THE CONFIG
-------------------------------------------
A setting passed to `create_engine` and never applied is indistinguishable from one that works,
until an outage. `tools/database_bounds_check.py` asks PostgreSQL what it thinks, and then proves
the bound is *enforced* by requesting a sleep longer than a deliberately tiny `statement_timeout`.

```
statement_timeout                   = 2min
idle_in_transaction_session_timeout = 5min
pg_sleep(5) with statement_timeout=250ms -> "canceling statement due to statement timeout"
```
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import settings  # noqa: E402


# ===========================================================================
# THE SETTINGS
# ===========================================================================
@pytest.mark.parametrize(
    "name",
    [
        "database_connect_timeout",
        "database_statement_timeout_ms",
        "database_idle_transaction_timeout_ms",
        "database_pool_recycle_seconds",
    ],
)
def test_every_database_bound_is_a_setting_and_is_positive(name):
    """Positive, because a bound of zero or None is the defect spelled as configuration.

    They are settings rather than constants because the right value depends on the deployment's
    network - and a bound nobody can tune is a bound somebody removes.
    """
    value = getattr(settings, name, None)
    assert value is not None, (
        f"{name} does not exist. Every one of these defaults means 'no limit' in the library, so "
        "the absence of the setting is the absence of the bound."
    )
    assert isinstance(value, int) and value > 0, (
        f"{name} is {value!r}; a non-positive value is not a bound"
    )


def test_the_bounds_are_generous_enough_not_to_break_legitimate_work():
    """A bound that is too tight is its own outage.

    Two minutes is a backstop against a runaway query, not a performance policy; five minutes
    idle-in-transaction is far longer than any real unit of work holds a transaction open.
    """
    assert settings.database_connect_timeout >= 5
    assert settings.database_statement_timeout_ms >= 30_000
    assert settings.database_idle_transaction_timeout_ms >= 60_000
    # Ten minutes would already be longer than most NAT idle timers, which is the point of it.
    assert 60 <= settings.database_pool_recycle_seconds <= 900


# ===========================================================================
# THE ENGINE
# ===========================================================================
def _startup_options_source() -> str:
    """The body of `_startup_options`, where the two server-side timeouts are built.

    Read with `ast`: the function's docstring explains the search_path hazard at length, and a text
    scan would be satisfied by the explanation rather than the code.
    """
    source = (BACKEND / "database.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_startup_options":
            body = [n for n in node.body if not isinstance(n, ast.Expr)]
            return ast.unparse(ast.Module(body=body, type_ignores=[]))
    raise AssertionError("database.py has no `_startup_options`")


def _engine_kwargs_for_postgres() -> str:
    """The PostgreSQL branch of `database.py`, as source.

    Read with `ast` rather than by string search: the module's own comments discuss the settings
    at length, and a text scan would be satisfied by the explanation instead of the code.
    """
    source = (BACKEND / "database.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if "sqlite" in test and len(node.orelse) == 1:
                return ast.unparse(node.orelse[0])
    raise AssertionError("database.py has no `else` branch for non-SQLite databases")


def test_the_postgres_engine_sets_a_connect_timeout():
    """THE trap. Without it, `pool_pre_ping` puts an OS-length timeout on every request."""
    branch = _engine_kwargs_for_postgres()
    assert "connect_timeout" in branch, (
        "the PostgreSQL engine sets no connect_timeout, so every connection checkout can block "
        "for the operating system's TCP timeout - and pool_pre_ping puts that on every request"
    )


def test_the_postgres_engine_sets_a_statement_timeout():
    """Nothing else bounds a query, and an unbounded query holds a pool slot."""
    options = _startup_options_source()
    assert "statement_timeout" in options
    assert "database_statement_timeout_ms" in options, (
        "the statement timeout is hard-coded rather than taken from the setting"
    )


def test_the_postgres_engine_sets_an_idle_transaction_timeout():
    """PostgreSQL defaults this to 0, so a leaked transaction holds locks forever."""
    assert "idle_in_transaction_session_timeout" in _startup_options_source()


def test_the_postgres_engine_recycles_connections():
    """Otherwise a connection killed by a NAT idle timer is reused and fails on use."""
    branch = _engine_kwargs_for_postgres()
    assert "pool_recycle" in branch
    assert "_startup_options" in branch, (
        "the PostgreSQL branch no longer builds its options through the helper, so the "
        "composition guarantee below is not in force"
    )


def test_the_metrics_engine_is_bounded_too():
    """`/metrics` is UNAUTHENTICATED.

    An unbounded connect there lets an anonymous request block a worker for the OS TCP timeout,
    which makes it the most exposed of the two engines rather than the least.
    """
    source = (BACKEND / "prometheus_metrics.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_engine_from"
    )
    body = ast.unparse(function)
    assert "connect_timeout" in body, "the metrics engine sets no connect timeout"
    assert "statement_timeout" in body or "_startup_options" in body, (
        "the metrics engine sets no statement timeout"
    )
    assert "pool_recycle" in body, "the metrics engine never recycles connections"


# ===========================================================================
# COMPOSITION: the regression this phase actually caused
# ===========================================================================
def test_startup_options_PRESERVE_what_the_url_already_sets():
    """THE regression, guarded.

    `connect_args` take precedence over the URL's query parameters. So passing
    `connect_args={"options": "-c statement_timeout=..."}` **discarded** the `options` a scoped URL
    already carried:

        postgresql://.../db?options=-csearch_path%3Dtenant_abc

    The search_path was gone, so the request-path isolation probe read `public`, row-level security
    denied everything, and every tenant query returned `[]`.

    **The symptom pointed the wrong way**: an isolation test failed because isolation was applied
    too strictly, not too loosely.
    """
    from database import _startup_options

    scoped = "postgresql://u:p@h/db?options=-csearch_path%3Dtenant_abc"
    composed = _startup_options(scoped)

    assert "search_path" in composed, (
        "the search_path the URL set has been discarded; every tenant query will read public and "
        "be denied by row-level security"
    )
    assert "tenant_abc" in composed, "the schema name was lost from the composed options"
    assert "statement_timeout" in composed, "the bound was lost while composing"
    assert "idle_in_transaction_session_timeout" in composed


def test_startup_options_work_when_the_url_sets_none():
    """The ordinary case must not be broken by the composition."""
    from database import _startup_options

    plain = _startup_options("postgresql://u:p@h/db")
    assert plain.startswith("-c statement_timeout=")
    assert "search_path" not in plain


def test_startup_options_survive_a_url_they_cannot_parse():
    """A URL this exotic is not worth failing startup over - but it must still be bounded."""
    from database import _startup_options

    composed = _startup_options("not a url at all")
    assert "statement_timeout" in composed


# ===========================================================================
# A server that is not running means SKIP, not FAIL
# ===========================================================================
#
# The static assertions above need no database, and must keep running without one. The behavioural
# ones below do - and when PostgreSQL is stopped they used to FAIL with a raw psycopg2
# `OperationalError`, which reads as a defect in the bounds rather than an absent dependency.
#
# A test that goes red because a dependency is not running is a test that cries wolf, and it makes
# coding without the database impossible. This fixture turns "not reachable" into a skip WITH the
# reason, and leaves a genuine query failure as a failure.
#: Tests that assert on SOURCE and settings, and need no server. Declared explicitly rather than
#: guessed from the name: the first version matched substrings of the name, missed one test, and
#: left a failure that looked like a defect in the bounds.
STATIC_TESTS = frozenset(
    {
        "test_every_database_bound_is_a_setting_and_is_positive",
        "test_the_bounds_are_generous_enough_not_to_break_legitimate_work",
        "test_the_postgres_engine_sets_a_connect_timeout",
        "test_the_postgres_engine_sets_a_statement_timeout",
        "test_the_postgres_engine_sets_an_idle_transaction_timeout",
        "test_the_postgres_engine_recycles_connections",
        "test_the_metrics_engine_is_bounded_too",
        "test_startup_options_PRESERVE_what_the_url_already_sets",
        "test_startup_options_work_when_the_url_sets_none",
        "test_startup_options_survive_a_url_they_cannot_parse",
    }
)


@pytest.fixture(autouse=True)
def _skip_behavioural_tests_when_postgres_is_down(request):
    """A server that is not running means SKIP, not FAIL.

    The static assertions need no database and keep running without one. The behavioural ones used
    to FAIL with a raw psycopg2 `OperationalError`, which reads as a defect in the bounds rather
    than an absent dependency - a test crying wolf, and a reason coding without the database is
    impossible.
    """
    if request.node.name.split("[")[0] in STATIC_TESTS:
        return

    url = _admin_url()
    if not url:
        pytest.skip("no PostgreSQL configured")

    from sqlalchemy import create_engine, exc, text

    engine = create_engine(url, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except exc.OperationalError as error:
        pytest.skip(f"PostgreSQL is not running, so there is nothing to check: {str(error)[:80]}")
    finally:
        engine.dispose()


# ===========================================================================
def _admin_url() -> str | None:
    """The admin URL from `.env`, or the environment. None when there is no PostgreSQL here."""
    from_env = os.environ.get("GRANADA_ADMIN_DATABASE_URL")
    if from_env and from_env.startswith("postgres"):
        return from_env
    env_file = BACKEND / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("GRANADA_ADMIN_DATABASE_URL="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value if value.startswith("postgres") else None
    return None


def _to_ms(value: str) -> int:
    """PostgreSQL renders these as '2min', '5min', '0', or '120000ms'."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    match = re.fullmatch(r"(\d+)\s*(ms|s|min|h)?", value)
    assert match, f"cannot parse {value!r}"
    unit = match.group(2) or "ms"
    return int(match.group(1)) * {"ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000}[unit]


def test_postgresql_reports_the_configured_bounds():
    """THE assertion that matters: the SERVER's opinion, not the config's.

    A setting passed to `create_engine` and never applied looks exactly like one that works.
    """
    url = _admin_url()
    if not url:
        pytest.skip("no PostgreSQL configured")

    from sqlalchemy import create_engine, text

    engine = create_engine(
        url,
        connect_args={
            "connect_timeout": settings.database_connect_timeout,
            "options": (
                f"-c statement_timeout={settings.database_statement_timeout_ms}"
                f" -c idle_in_transaction_session_timeout="
                f"{settings.database_idle_transaction_timeout_ms}"
            ),
        },
    )
    try:
        with engine.connect() as connection:
            statement = connection.execute(text("SHOW statement_timeout")).scalar()
            idle = connection.execute(
                text("SHOW idle_in_transaction_session_timeout")
            ).scalar()
    finally:
        engine.dispose()

    assert _to_ms(statement) == settings.database_statement_timeout_ms, (
        f"PostgreSQL reports statement_timeout={statement!r}, not the configured "
        f"{settings.database_statement_timeout_ms}ms"
    )
    assert _to_ms(idle) == settings.database_idle_transaction_timeout_ms, (
        f"PostgreSQL reports idle_in_transaction_session_timeout={idle!r}, not the configured "
        f"{settings.database_idle_transaction_timeout_ms}ms"
    )


def test_postgresql_actually_ENFORCES_the_statement_timeout():
    """Reported is not the same as enforced.

    A deliberately tiny bound and a sleep longer than it. If the statement returns, the setting is
    decoration.
    """
    url = _admin_url()
    if not url:
        pytest.skip("no PostgreSQL configured")

    from sqlalchemy import create_engine, exc, text

    engine = create_engine(url, connect_args={"options": "-c statement_timeout=250"})
    try:
        with pytest.raises(exc.DBAPIError) as caught:
            with engine.connect() as connection:
                connection.execute(text("SELECT pg_sleep(5)"))
    finally:
        engine.dispose()

    message = str(caught.value.orig).lower()
    assert "statement timeout" in message or "57014" in message, (
        f"pg_sleep(5) failed for an unexpected reason, so this proves nothing: {message[:150]}"
    )


def test_the_settings_are_not_so_tight_that_a_normal_query_fails():
    """The counterpart: a bound tight enough to break ordinary work is its own outage.

    This is a real query against the real database, not a sleep - the point is that the configured
    timeout leaves room for the work the application actually does.
    """
    url = _admin_url()
    if not url:
        pytest.skip("no PostgreSQL configured")

    from sqlalchemy import create_engine, text

    engine = create_engine(
        url,
        connect_args={
            "options": f"-c statement_timeout={settings.database_statement_timeout_ms}"
        },
    )
    try:
        with engine.connect() as connection:
            # Something with real work in it: a join and an aggregate over the catalogue.
            connection.execute(
                text("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace")
            ).scalar()
    finally:
        engine.dispose()
