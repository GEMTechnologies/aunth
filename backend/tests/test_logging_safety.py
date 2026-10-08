"""A `logger.extra` key that collides with a LogRecord attribute crashes the request.

THE BUG THIS PREVENTS, found only in production
-----------------------------------------------
`POST /api/v1/ingest/opportunities` returned **500 Internal Server Error** on the first real producer
delivery, *after* the batch had already been ingested - so the data landed and the caller was told it
had failed:

    File "/app/backend/ingestion_api.py", line 229, in ingest_opportunities
        logger.info(
    File "logging/__init__.py", line 1686, in makeRecord
    KeyError: "Attempt to overwrite 'created' in LogRecord"

`LogRecord` already has `created` (a timestamp). `Logger.makeRecord` raises when `extra` collides.

WHY NO TEST CAUGHT IT
---------------------
`Logger._log` checks the level BEFORE building the record:

    if self.isEnabledFor(level):
        record = self.makeRecord(...)

The root level under pytest is WARNING, so `logger.info(...)` was a **no-op in every test** and the
line only executed once INFO logging was enabled in production. A test that merely exercised the
endpoint could never have found this, which is why the guard below reads the SOURCE instead of
running the code.

Reserved names come from `logging.LogRecord` itself rather than a hardcoded list, so this keeps
working if the standard library adds one.
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

#: Every attribute a LogRecord already carries. `extra` may not use these.
RESERVED = frozenset(logging.LogRecord("n", 1, "p", 1, "m", None, None).__dict__.keys())

#: Modules to scan. Excludes the test suite itself: a guard that inspects its own source finds its
#: own docstring, which is a mistake this repository has made before.
SCANNED = (
    "agent/fleet_runner.py",
    "events/relay.py",
    "ingestion_api.py",
    "agent_api.py",
    "main.py",
)


def _literal_keys(node: ast.AST) -> list[str]:
    """String keys of a dict literal, ignoring anything computed."""
    if not isinstance(node, ast.Dict):
        return []
    return [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]


def _extra_keys(call: ast.Call) -> list[str]:
    for keyword in call.keywords:
        if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
            return _literal_keys(keyword.value)
    return []


def _logger_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("debug", "info", "warning", "error", "critical", "exception", "log")
            and isinstance(func.value, ast.Name)
            and func.value.id in ("logger", "log", "LOGGER")
        ):
            yield node


@pytest.mark.parametrize("relative", SCANNED)
def test_no_logger_extra_key_collides_with_a_LogRecord_attribute(relative):
    """THE guard.

    A collision is not a warning: `makeRecord` raises, the exception propagates out of the request
    handler, and a 500 is returned for work that had already succeeded.
    """
    path = BACKEND / relative
    if not path.exists():
        pytest.skip(f"{relative} is not present")

    collisions: list[str] = []
    for call in _logger_calls(ast.parse(path.read_text(encoding="utf-8"))):
        for key in _extra_keys(call):
            if key in RESERVED:
                collisions.append(f"{relative}:{call.lineno} uses extra={{'{key}': ...}}")

    assert not collisions, (
        "these logger calls use a key that LogRecord already defines, which raises KeyError at "
        "runtime and turns a successful request into a 500:\n  " + "\n  ".join(collisions)
    )


def test_the_guard_finds_a_collision_when_there_is_one():
    """INVERTED, because a guard that cannot fail proves nothing.

    Feeds the scanner a source that DOES collide, and requires it to be found. Without this, a
    refactor that quietly stopped detecting anything would leave the guard green and useless.
    """
    source = 'logger.info("x", extra={"created": 1, "module": "y"})'
    tree = ast.parse(source)
    found = [key for call in _logger_calls(tree) for key in _extra_keys(call) if key in RESERVED]
    assert "created" in found and "module" in found


def test_the_guard_does_not_flag_a_safe_key():
    """The inverse, so the guard is not satisfied by rejecting everything."""
    tree = ast.parse('logger.info("x", extra={"created_count": 1, "received_count": 2})')
    found = [key for call in _logger_calls(tree) for key in _extra_keys(call) if key in RESERVED]
    assert found == []


def test_the_ingestion_logger_call_specifically_is_safe():
    """The exact site of the production failure, pinned by name.

    The parametrised test above would catch a regression, but naming the call means a reader
    replacing `created_count` with `created` gets an error that says why.
    """
    source = (BACKEND / "ingestion_api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for call in _logger_calls(tree):
        keys = _extra_keys(call)
        if "received_count" in keys:
            assert not (set(keys) & RESERVED)
            return
    pytest.fail("the ingest.batch logger call no longer uses the safe key names")
