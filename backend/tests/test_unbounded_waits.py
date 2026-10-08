"""A wait that cannot be bounded is an outage waiting for a trigger.

THE DEFECT
----------
`redis-py 5.0.1` defaults `socket_timeout` and `socket_connect_timeout` to **None**, which means
*block forever*. `events/publisher.py` built its client with neither:

    redis.Redis.from_url(self._url, decode_responses=True)

One client, three places that hang on it:

* the relay's `xadd` — the outbox stops draining, silently, and nothing raises;
* the consumer's `xreadgroup` — the fleet stops receiving work;
* `health.check_redis()`'s `ping()` — and `/readyz` calls that, so a Redis that **accepts a
  connection and then stops answering** makes the readiness endpoint that a load balancer and an
  orchestrator depend on hang forever.

That is not hypothetical here: `tools/redis_restart_probe.py` exists because Redis restarts are a
known concern in this deployment.

A second, sharper version of the same mistake: `HttpxTransport.__init__` accepted `timeout`
straight into `self.timeout` and the request path used `timeout or self.timeout`. So
`HttpxTransport(timeout=None)` turned a bounded send into `httpx.Client(timeout=None)` — wait
forever — on the class whose own docstring says *"a send that hangs holds a worker"*.

WHY THE BLACK HOLE
------------------
Testing this against a Redis that refuses connections proves nothing: a refused connection returns
instantly. The failure being guarded is a socket that **opens and then goes silent**, which is what
a wedged instance looks like from the client. So these tests stand up a TCP server that accepts
connections and never writes a byte, and then assert the call RETURNS.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

ROOT = BACKEND.parent.parent


class BlackHole:
    """A TCP server that accepts connections and never answers.

    This is the shape of a wedged Redis: the handshake succeeds, so nothing looks wrong, and every
    read blocks until the client gives up - or forever, if it has no socket timeout.
    """

    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(16)
        self.port = self.socket.getsockname()[1]
        self._accepted: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        self.socket.settimeout(0.5)
        while not self._stop.is_set():
            try:
                connection, _ = self.socket.accept()
            except (socket.timeout, OSError):
                continue
            # Accepted, and deliberately never written to.
            self._accepted.append(connection)

    def __enter__(self) -> "BlackHole":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        # Closing the accepted sockets is what unblocks anything still reading, so an abandoned
        # thread does not outlive the test.
        for connection in self._accepted:
            try:
                connection.close()
            except OSError:
                pass
        try:
            self.socket.close()
        except OSError:
            pass

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"


# ===========================================================================
# BEHAVIOURAL: the call returns
# ===========================================================================
#: How long THIS TEST waits for the bounded client to give up.
#:
#: The test's own deadline, not the client's. It must exceed the client timeout comfortably and be
#: finite regardless of what the client does - otherwise a regression hangs the suite instead of
#: failing it.
TEST_DEADLINE_SECONDS = 25.0


def test_the_bounded_client_gives_up_while_the_unbounded_one_does_not():
    """THE defect, as a direct A/B against the same wedged Redis.

    `health.check_redis()` calls exactly `publisher.client.ping()`, and `/readyz` calls that - so a
    Redis that opens a socket and goes silent used to hang the readiness endpoint that a load
    balancer and an orchestrator depend on.

    Both clients talk to the SAME black hole at once. The bounded one must give up; the unbounded
    one, built the way the code used to build it, must still be waiting. Without the second half
    this test would pass against a server that merely refuses connections - which returns instantly
    and proves nothing.

    THE BOUND IS THE TEST'S OWN. Running `ping()` inline made the test hang rather than fail when
    the timeout was removed, because it was waiting on the property it exists to check.
    """
    import redis

    from events.publisher import RedisEventPublisher

    with BlackHole() as hole:
        # Marked so the repository-wide scan skips THIS call: it is deliberately the unbounded
        # client, and it is what makes the assertion meaningful.
        unbounded = redis.Redis.from_url(  # bounded-check: deliberate-unbounded
            hole.url, decode_responses=True
        )
        # redis-py OMITS a kwarg that was never passed, so "block forever" is an ABSENT key, not a
        # None value. Checking only for None was my first version and it raised KeyError.
        kwargs = unbounded.connection_pool.connection_kwargs
        assert kwargs.get("socket_timeout") is None, (
            "redis-py no longer leaves socket_timeout unset, so this comparison is stale"
        )

        unbounded_finished = threading.Event()

        def attempt_unbounded() -> None:
            try:
                unbounded.ping()
            except Exception:  # noqa: BLE001
                pass
            unbounded_finished.set()

        threading.Thread(target=attempt_unbounded, daemon=True).start()

        outcome: dict[str, object] = {}

        def attempt_bounded() -> None:
            started = time.time()
            try:
                RedisEventPublisher(hole.url).client.ping()
            except Exception as exc:  # noqa: BLE001 - giving up is the expected result
                outcome["error"] = type(exc).__name__
            outcome["elapsed"] = time.time() - started
            outcome["done"] = True

        threading.Thread(target=attempt_bounded, daemon=True).start()

        # THE TEST'S OWN BOUND. Whatever the client does, this loop ends.
        waited = 0.0
        while waited < TEST_DEADLINE_SECONDS and "done" not in outcome:
            time.sleep(0.1)
            waited += 0.1

        gave_up = "done" in outcome
        still_waiting = not unbounded_finished.is_set()

    assert gave_up, (
        f"the bounded client had not given up after {TEST_DEADLINE_SECONDS:.0f}s on a wedged "
        "Redis. Its socket timeout is missing or ineffective, and behind /readyz that is a "
        "readiness endpoint that never responds."
    )
    assert float(outcome["elapsed"]) < 15, (
        f"the bounded client took {outcome['elapsed']}s to give up, which is longer than the "
        "single-digit timeout it should have"
    )
    assert still_waiting, (
        "the UNBOUNDED client returned, so the black hole is not behaving like a wedged Redis and "
        "this test proves nothing about the bounded one."
    )
    # `__exit__` closes the accepted sockets, which unblocks the abandoned thread.


def test_the_publisher_client_sets_both_socket_timeouts():
    """The fix, asserted on the real object rather than on the source text."""
    from events.publisher import (
        SOCKET_CONNECT_TIMEOUT_SECONDS,
        SOCKET_TIMEOUT_SECONDS,
        RedisEventPublisher,
    )

    publisher = RedisEventPublisher("redis://127.0.0.1:6379/0")
    kwargs = publisher.client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == SOCKET_TIMEOUT_SECONDS, (
        "socket_timeout is not set, so every operation on this client can block forever"
    )
    assert kwargs["socket_connect_timeout"] == SOCKET_CONNECT_TIMEOUT_SECONDS


# ===========================================================================
# The HTTP side: a timeout that could be disabled
# ===========================================================================
def test_the_http_transport_refuses_a_timeout_of_None():
    """`httpx.Client(timeout=None)` means WAIT FOREVER.

    `HttpxTransport.__init__` accepted `timeout` straight through, so passing None silently
    removed the bound - on the class whose docstring says "a send that hangs holds a worker".
    """
    from agent.mail.providers.http import HttpxTransport

    with pytest.raises(ValueError):
        HttpxTransport(timeout=None)


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_the_http_transport_refuses_a_non_positive_timeout(bad):
    """Zero and negative are the same defect spelled differently: no usable bound."""
    from agent.mail.providers.http import HttpxTransport

    with pytest.raises(ValueError):
        HttpxTransport(timeout=bad)


def test_the_http_transport_still_accepts_a_real_timeout():
    """The fix must not have broken the ordinary path."""
    from agent.mail.providers.http import DEFAULT_TIMEOUT_SECONDS, HttpxTransport

    assert HttpxTransport().timeout == DEFAULT_TIMEOUT_SECONDS
    assert HttpxTransport(timeout=5.0).timeout == 5.0


# ===========================================================================
# STATIC: every client in the repository is bounded
# ===========================================================================
def _redis_client_calls():
    """Every Redis client construction, found with `ast`.

    Not with a regex over the source text: the docstring above QUOTES the defect verbatim, and a
    text scan flags the explanation instead of the mistake. A guard that reports its own
    documentation is a guard that gets switched off.
    """
    import ast

    found = []
    for path in list((ROOT / "tools").rglob("*.py")) + list(
        (ROOT / "Auth" / "backend").rglob("*.py")
    ):
        if any(part in {".venv", "__pycache__", "node_modules"} for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            dotted = ast.unparse(func)
            if not (dotted.startswith("redis.Redis") or dotted.startswith("redis.StrictRedis")):
                continue
            # A declared exception, on the line of the call itself: the deliberate unbounded
            # client in the comparison test above.
            line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
            if "deliberate-unbounded" in line:
                continue
            found.append((path, node, ast.unparse(node)))
    return found


def test_the_scan_finds_the_redis_clients():
    """So the assertion below cannot pass by finding nothing."""
    calls = _redis_client_calls()
    assert len(calls) >= 2, (
        f"only {len(calls)} Redis client constructions found; the scan is broken"
    )


def test_every_redis_client_in_the_repository_sets_socket_timeouts():
    """redis-py defaults both socket timeouts to None, which means *block forever*.

    That is not an error anywhere: it is a client that waits indefinitely, and nothing says so.
    A new client is the likely way this defect comes back.
    """
    offenders = []
    for path, node, rendered in _redis_client_calls():
        if "socket_timeout" not in rendered or "socket_connect_timeout" not in rendered:
            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "these Redis clients set no socket timeouts, so every operation on them can block "
        "forever (redis-py defaults both to None):\n  " + "\n  ".join(offenders)
    )


def _httpx_client_calls():
    """Every httpx client construction, found with `ast` for the same reason."""
    import ast

    found = []
    for path in (ROOT / "Auth" / "backend").rglob("*.py"):
        if any(part in {".venv", "__pycache__", "node_modules"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func) in {"httpx.Client", "httpx.AsyncClient"}:
                found.append((path, node, ast.unparse(node)))
    return found


def test_the_scan_finds_the_httpx_clients():
    calls = _httpx_client_calls()
    assert len(calls) >= 3, f"only {len(calls)} httpx clients found; the scan is broken"


def test_every_httpx_client_construction_is_bounded():
    """`httpx.Client()` defaults to 5s, so this is about visibility rather than survival.

    An explicit timeout is a decision a reader can see; a library default is one that changes with
    the library, and `timeout=None` - which `HttpxTransport` accepted until this phase - means wait
    forever.
    """
    offenders = []
    for path, node, rendered in _httpx_client_calls():
        if "timeout" not in rendered:
            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "these httpx clients rely on the library default timeout:\n  " + "\n  ".join(offenders)
    )
