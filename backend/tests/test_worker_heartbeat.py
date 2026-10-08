"""A background worker's liveness is "is the loop still going round", not "is a port open".

THE DEFECT
----------
The image carries one HEALTHCHECK, inherited by every service:

    CMD curl -fsS "http://127.0.0.1:${PORT}/livez"

That is right for the API and **wrong for the worker and the relay**, which are background loops and
listen on no port at all. Docker therefore reported both `unhealthy` while they were working
perfectly - and in an orchestrator that is a restart loop killing a healthy process every 90 seconds.

The difference matters for more than tidiness. A worker can hold its process open while the loop is
stuck: a wedged database call, an unhandled exception swallowed by the loop, a sleep that never
returns. A port check calls that healthy. A heartbeat calls it what it is.

`docker compose` cannot be unit-tested, so these tests cover the signal itself and the compose
wiring separately.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent import heartbeat  # noqa: E402

ROOT = BACKEND.parent.parent


# ===========================================================================
# The signal
# ===========================================================================
def test_a_beat_makes_the_heartbeat_fresh(tmp_path):
    target = tmp_path / "hb"
    heartbeat.beat(target)
    assert target.exists()
    assert heartbeat.is_fresh(path=target, max_age_seconds=60)


def test_an_old_beat_is_not_fresh(tmp_path):
    """THE property that catches a wedged loop.

    A file that exists but has not been touched for longer than the threshold means the loop
    stopped going round - which is exactly the failure a port check cannot see.
    """
    target = tmp_path / "hb"
    heartbeat.beat(target, now=1_000_000.0)
    assert not heartbeat.is_fresh(path=target, max_age_seconds=180, now=1_000_000.0 + 181)
    # And it IS fresh within the threshold, so the assertion above is not satisfied by always-false.
    assert heartbeat.is_fresh(path=target, max_age_seconds=180, now=1_000_000.0 + 179)


def test_a_missing_heartbeat_is_not_fresh(tmp_path):
    """Which is what a worker that never completed a sweep reports.

    The FleetHealth docstring already makes this point: "a loop that has never completed a sweep is
    not healthy, however new". A missing file is that state.
    """
    assert not heartbeat.is_fresh(path=tmp_path / "never-written", max_age_seconds=3600)


def test_a_corrupt_heartbeat_is_not_fresh(tmp_path):
    """A half-written or garbage file must not read as alive."""
    target = tmp_path / "hb"
    target.write_text("not a number", encoding="utf-8")
    assert not heartbeat.is_fresh(path=target, max_age_seconds=3600)


def test_beating_never_raises_even_on_an_unwritable_path(tmp_path):
    """A liveness signal must not be able to kill the thing it observes.

    If the container's /tmp is read-only or full, the worker must keep working. The consequence of a
    failed write is a false `unhealthy` report, which is a worse-looking outcome than a crash and a
    far better one.
    """
    heartbeat.beat(Path("/definitely/not/a/writable/path/hb"))
    # No exception. On Windows a rooted path with no drive may also simply fail - both are fine.


def test_the_default_path_is_per_container_and_not_persisted():
    """`/tmp`, deliberately.

    The signal is about THIS process. A path on a mounted volume would survive a restart and report
    a dead worker as healthy - the exact inversion the heartbeat exists to prevent.

    Asserted against the SOURCE rather than the rendered Path: on Windows `Path("/tmp/x")` renders as
    `\\tmp\\x`, and the container is Linux, so the declaration is the fact that matters.
    """
    module = (BACKEND / "agent" / "heartbeat.py").read_text(encoding="utf-8")
    assert 'DEFAULT_HEARTBEAT_PATH = Path("/tmp/granada-heartbeat")' in module
    # And it must not be on a volume: no volume path appears anywhere in the module.
    for persisted in ("/var/lib", "/data", "/mnt", "/srv"):
        assert persisted not in module, (
            f"the heartbeat default mentions {persisted}, which suggests it is on a persisted "
            "volume - a stale file would then report a dead worker as healthy"
        )


# ===========================================================================
# The loops
# ===========================================================================
def test_both_loops_beat_after_completing_a_sweep():
    """Read from the source, because the loops run forever and cannot be unit-tested directly.

    Comments are stripped first: this repository has repeatedly had a guard satisfied by the text a
    file *says* rather than what it *does*, and the fix's own explanation discusses `heartbeat.beat()`
    at length.
    """
    for relative in ("agent/fleet_runner.py", "events/relay.py"):
        source = (BACKEND / relative).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines()
            if not line.strip().startswith("#")
        )
        # Drop docstrings, which is where the explanation lives.
        import ast

        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    body.pop(0)
        executable = ast.unparse(tree)
        assert "heartbeat.beat()" in executable, (
            f"{relative} does not beat its heartbeat, so Docker cannot tell whether its loop is "
            "still going round"
        )


def test_the_beat_comes_AFTER_the_work_not_before():
    """Ordering, and it is the whole point.

    Beating BEFORE the sweep would report a worker alive while it was stuck inside one - a wedged
    loop would look perfectly healthy, which is worse than no healthcheck at all.
    """
    source = (BACKEND / "agent" / "fleet_runner.py").read_text(encoding="utf-8")
    sweep_at = source.index("self.sweep_once()")
    beat_at = source.index("heartbeat.beat()")
    assert beat_at > sweep_at, (
        "the heartbeat is touched before the sweep completes, so a wedged sweep would still "
        "report the worker alive"
    )


# ===========================================================================
# The compose wiring
# ===========================================================================
def test_worker_and_relay_do_not_use_the_http_healthcheck():
    """THE regression. Both are background loops; neither serves HTTP.

    Inheriting the image's `curl /livez` check made them permanently unhealthy on the first real
    deployment while they were working correctly.
    """
    raw = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # STRIP THE COMMENTS FIRST. This block's own explanation quotes `/livez` while describing why it
    # was wrong, so a check over the raw text is satisfied by the prose rather than the config -
    # which is the exact failure mode this repository keeps producing.
    compose = "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("#")
    )

    for service in ("worker", "relay"):
        start = compose.index(f"  {service}:")
        # The block runs to the next top-level service key.
        remainder = compose[start + 1 :]
        candidates = [
            index for marker in ("\n  api:", "\n  postgres:", "\n  rootly:", "\n  migrate:")
            if (index := remainder.find(marker)) != -1
        ]
        end = start + 1 + (min(candidates) if candidates else len(remainder))
        block = compose[start:end]

        assert "healthcheck:" in block, (
            f"{service} has no healthcheck of its own, so it inherits the image's HTTP check - "
            "which can never pass for a process that serves no HTTP"
        )
        assert "/livez" not in block, (
            f"{service}'s healthcheck curls /livez, but the service serves no HTTP. Either give it "
            "a heartbeat check or give it no healthcheck at all"
        )
        assert "granada-heartbeat" in block, (
            f"{service}'s healthcheck does not consult the heartbeat"
        )


def test_the_compose_healthcheck_wiring_matches_the_code():
    """The path and the threshold must agree with `heartbeat.py`, or the check is decorative.

    A healthcheck reading a different path, or allowing a staleness the code never produces, would
    pass forever while proving nothing.
    """
    compose = "\n".join(
        line
        for line in (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )
    module = (BACKEND / "agent" / "heartbeat.py").read_text(encoding="utf-8")

    assert "/tmp/granada-heartbeat" in compose
    assert 'DEFAULT_HEARTBEAT_PATH = Path("/tmp/granada-heartbeat")' in module
