"""The relay must drain per organisation when the fleet role cannot see across tenants.

THE DEFECT THIS PINS. `outbox_events` has RLS ENABLED (not forced), so the table owner bypasses the
policy but the fleet role does not. ADR-0011 revoked `granada_fleet`'s BYPASSRLS, and the relay kept
connecting as that role with no tenant binding - so its SELECT returned ZERO rows and it published
nothing while reporting `pending: 0`.

Measured on production: 1089 unpublished events, ALL with `attempts = 0` (never even tried), the
oldest seven days old, and a relay logging `nothing_published pending: 0` every five seconds. The
health line was itself the defect: it re-ran the same blind query and reported its own blindness as
calm.

WHAT MUST HOLD AFTER THE FIX:
  * the binding is applied BEFORE each organisation is drained, not once per sweep - a drain commits
    and the pool resets the binding, so a single binding at the top would leave every organisation
    after the first unseen;
  * the binding is CLEARED after every organisation, including on failure, because a leaked binding
    would drain the next organisation's outbox under this one's context.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from events import relay as relay_module  # noqa: E402
from events.relay import OutboxRelayRunner  # noqa: E402


class RecordingSession:
    """Records every statement so the binding order is inspectable."""

    def __init__(self, orgs=("org-a", "org-b")):
        self.orgs = list(orgs)
        self.statements: list[tuple[str, dict | None]] = []
        self.commits = 0
        self.closed = False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        if "fleet_active_agent_ids" in sql:
            return _Rows([(org,) for org in self.orgs])
        return _Rows([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)


def _bindings(session: RecordingSession) -> list[str]:
    out = []
    for sql, params in session.statements:
        if "set_config" in sql and params and "org" in params:
            out.append(params["org"])
    return out


@pytest.fixture
def drained(monkeypatch):
    """Replace OutboxRelay so the test is about ORDER, not about publishing."""
    calls: list[str] = []

    class FakeRelay:
        def __init__(self, db, publisher, batch_size=100):
            self.db = db

        def drain_once(self):
            calls.append("drain")
            return 1

    monkeypatch.setattr(relay_module, "OutboxRelay", FakeRelay)
    return calls


def test_an_unbound_relay_does_not_bind(drained):
    session = RecordingSession()
    runner = OutboxRelayRunner(lambda: session, publisher=object())
    runner.use_tenant_binding = False
    assert runner.sweep_once() == 1
    assert _bindings(session) == []


def test_a_bound_relay_binds_before_each_organisation(drained):
    session = RecordingSession(orgs=("org-a", "org-b"))
    runner = OutboxRelayRunner(lambda: session, publisher=object())
    runner.use_tenant_binding = True

    assert runner.sweep_once() == 2, "one drain per organisation"

    bindings = _bindings(session)
    # bind a, clear, bind b, clear - interleaved, not all at the top.
    assert bindings == ["org-a", "", "org-b", ""], (
        "the binding must be re-applied per organisation: a drain commits and the pool resets it"
    )


def test_the_binding_is_cleared_even_when_a_drain_fails(monkeypatch):
    session = RecordingSession(orgs=("org-a",))

    class ExplodingRelay:
        def __init__(self, db, publisher, batch_size=100):
            pass

        def drain_once(self):
            raise RuntimeError("publisher gone")

    monkeypatch.setattr(relay_module, "OutboxRelay", ExplodingRelay)
    runner = OutboxRelayRunner(lambda: session, publisher=object())
    runner.use_tenant_binding = True

    with pytest.raises(RuntimeError):
        runner.sweep_once()

    assert _bindings(session) == ["org-a", ""], (
        "a leaked binding would drain the next organisation's outbox under this one's context"
    )


def test_no_organisations_is_not_an_error(drained):
    session = RecordingSession(orgs=())
    runner = OutboxRelayRunner(lambda: session, publisher=object())
    runner.use_tenant_binding = True
    assert runner.sweep_once() == 0
    assert _bindings(session) == []


def test_the_session_is_always_closed(drained):
    session = RecordingSession()
    runner = OutboxRelayRunner(lambda: session, publisher=object())
    runner.use_tenant_binding = True
    runner.sweep_once()
    assert session.closed is True


def test_the_flag_is_read_from_the_same_variable_the_fleet_reads(monkeypatch):
    """Two flags for one migration would drift; one flag read twice cannot."""
    monkeypatch.setenv("FLEET_TENANT_BINDING", "1")
    runner = OutboxRelayRunner(lambda: None, publisher=object())
    assert runner.use_tenant_binding is True

    monkeypatch.setenv("FLEET_TENANT_BINDING", "0")
    assert OutboxRelayRunner(lambda: None, publisher=object()).use_tenant_binding is False
