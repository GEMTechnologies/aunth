"""Funding discovery as a scheduled part of the fleet loop.

WHY THIS IS TESTED SEPARATELY FROM THE SOURCE. The source knows how to talk to grants.gov; the loop
knows when to ask. The failure that matters here is not a bad mapping - it is a sweep that either
hammers somebody else's API on every restart, or dies in a way that takes the dispatch sweep with it.
Both are tested without any network access, because neither is about the network.

THE DISABLED PATH IS THE DEFAULT FOR TESTS. `discovery_interval_seconds=0` turns the sweep off
entirely, which is how an air-gapped or test deployment avoids outbound requests without a code
branch. That it stays off is itself worth pinning: a scheduler that starts calling a federal API
because a class was constructed would be a surprise nobody asked for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import fleet_runner
from agent.fleet_runner import FleetRunner


class FakeSession:
    def __init__(self):
        self.closed = False
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class FakeSummary:
    def __init__(self, *, found=0, stored=0, errors=None):
        self.found = found
        self.stored = stored
        self.errors = list(errors or [])


def make_runner(**kwargs):
    return FleetRunner(FakeSession, **kwargs)


# ---------------------------------------------------------------------------
# Off unless asked for
# ---------------------------------------------------------------------------
def test_discovery_is_off_when_the_interval_is_zero():
    runner = make_runner(discovery_interval_seconds=0)
    assert runner._discovery_due() is False
    assert runner.refresh_funding_catalogue() == 0


def test_discovery_is_on_by_default():
    """The point of the work: a deployment gets a funding search without opting in."""
    runner = make_runner()
    assert runner.discovery_interval_seconds > 0


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------
def test_the_first_check_schedules_rather_than_sweeps():
    """A worker that restarts in a loop must not hit the funding API on every boot: the first check
    records the starting point and the first sweep happens one interval later."""
    runner = make_runner(discovery_interval_seconds=3600)
    assert runner._discovery_due() is False
    assert runner._last_discovery_at is not None


def test_a_sweep_becomes_due_after_the_interval():
    runner = make_runner(discovery_interval_seconds=3600)
    runner._last_discovery_at = datetime.now(timezone.utc) - timedelta(seconds=3601)
    assert runner._discovery_due() is True


def test_a_sweep_is_not_due_before_the_interval():
    runner = make_runner(discovery_interval_seconds=3600)
    runner._last_discovery_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    assert runner._discovery_due() is False


# ---------------------------------------------------------------------------
# Counters and failure isolation
# ---------------------------------------------------------------------------
def test_a_successful_sweep_is_counted(monkeypatch):
    called = {}

    def fake_run_and_commit(db, **kwargs):
        called.update(kwargs)
        return FakeSummary(found=8, stored=8)

    monkeypatch.setattr("agent.opportunity_discovery.run_and_commit", fake_run_and_commit)

    runner = make_runner(discovery_interval_seconds=3600)
    assert runner.refresh_funding_catalogue() == 8
    assert runner.health.discovery_sweeps == 1
    assert runner.health.discovery_found == 8
    assert runner.health.discovery_stored == 8
    assert runner.health.discovery_last_error is None
    assert runner._last_discovery_at is not None
    assert called["limit"] >= 1


def test_a_source_error_is_recorded_but_still_counts_the_sweep(monkeypatch):
    """A sweep that reached the source but got an error is a real outcome, not a crash: the found
    count is still worth having and the error must be visible to an operator."""
    def fake(db, **kwargs):
        return FakeSummary(found=0, stored=0, errors=["grants.gov is down"])

    monkeypatch.setattr("agent.opportunity_discovery.run_and_commit", fake)

    runner = make_runner(discovery_interval_seconds=3600)
    runner.refresh_funding_catalogue()
    assert runner.health.discovery_sweeps == 1
    assert "down" in (runner.health.discovery_last_error or "")


def test_an_exploding_sweep_never_escapes(monkeypatch):
    """THE FAILURE THAT MATTERS. Discovery is the least urgent thing in the loop; if it could raise,
    a dead funding API would take the dispatch sweep down with it and no work would run at all."""
    def boom(db, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("agent.opportunity_discovery.run_and_commit", boom)

    runner = make_runner(discovery_interval_seconds=3600)
    assert runner.refresh_funding_catalogue() == 0
    assert "kaboom" in (runner.health.discovery_last_error or "")
    assert runner.health.running is False, "the loop is still fine, it simply recorded the failure"


def test_an_unreachable_database_never_escapes():
    def broken_factory():
        raise RuntimeError("database unavailable")

    runner = FleetRunner(broken_factory, discovery_interval_seconds=3600)
    assert runner.refresh_funding_catalogue() == 0
    assert "unavailable" in (runner.health.discovery_last_error or "")


def test_the_session_is_always_closed(monkeypatch):
    sessions = []

    def factory():
        session = FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(
        "agent.opportunity_discovery.run_and_commit",
        lambda db, **kwargs: FakeSummary(found=1, stored=1),
    )

    runner = FleetRunner(factory, discovery_interval_seconds=3600)
    runner.refresh_funding_catalogue()
    assert len(sessions) == 1 and sessions[0].closed is True


def test_health_exposes_the_discovery_counters():
    """Invisible counters are how a bot dies quietly for a week: an operator has to be able to read
    last week's outcome off the health payload."""
    payload = make_runner(discovery_interval_seconds=3600).health.as_dict()
    assert "discovery_sweeps" in payload
    assert "discovery_found" in payload
    assert "discovery_stored" in payload
    assert "discovery_last_error" in payload
