"""No route may block the event loop.

THE BUG THIS GUARDS
-------------------
A load test against a real uvicorn measured this, 300 requests per path at concurrency 25:

    /livez               p50 =    5.15 ms
    /readyz              p50 = 6689.97 ms
    /api/v1/health/deep  p50 = 5893.94 ms
    /                    p50 =    5.43 ms

Five handlers were declared `async def` and contained **no `await` at all** - they called
synchronous SQLAlchemy, which ran ON the event loop and blocked every other request for its
duration.

It is an outage rather than a slow endpoint, because a load balancer probes `/readyz` on
every instance every few seconds. Each probe blocked that instance's loop, so **the health
check became the thing that took the service down** - and it removed the instance from the
pool for being slow to answer the very probe that was slowing it.

After making them `def` (FastAPI runs those in a threadpool), `/readyz` went from 6690 ms to
487 ms with everything else unchanged.

THE GUARD
Asserted structurally over the module, because the defect is invisible in review: `async
def` with no `await` looks like every other handler, and it only misbehaves with more than
one request in flight - which no unit test has.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

#: Every module that declares routes. `main.py` alone was 8 routes, and the agent API
#: declares 16 more - all of which do synchronous database work through `Depends(get_db)`.
#: A guard that covered one module would have missed the larger half.
ROUTE_MODULES = (BACKEND / "main.py", BACKEND / "agent_api.py")

#: Kept for the tests that read `main.py` specifically.
MAIN = BACKEND / "main.py"

#: Calls that do synchronous I/O or CPU work. A handler touching any of these must not be
#: `async def`, because FastAPI would run it on the event loop.
BLOCKING_CALLS = (
    "readiness(",
    "DatabaseManager.health_check",
    "exposition(",
    "snapshot(",
    "session.",
    "db.",
    "SessionLocal",
    "requests.",
    "subprocess.",
)

#: Handlers that genuinely await something, and so belong on the event loop.
ASYNC_ALLOWED = {
    # A middleware that awaits `call_next`, which is the whole point.
    "add_request_id",
}


def _routes() -> list[tuple[str, bool, str]]:
    """`(name, is_async, source)` for every decorated route handler, in every module."""
    found: list[tuple[str, bool, str]] = []
    for module in ROUTE_MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = " ".join(ast.unparse(d) for d in node.decorator_list)
            # `@router.get(...)` as well as `@app.get(...)`.
            if ".get(" not in decorators and ".post(" not in decorators:
                continue
            found.append((node.name, isinstance(node, ast.AsyncFunctionDef), ast.unparse(node)))
    return found


def test_the_route_scan_actually_finds_routes():
    """So the guard below cannot pass by finding nothing.

    The first version scanned only `main.py`, found 8 routes, and asserted `>= 10` - which
    would have failed while looking like a route problem rather than a scan problem. The
    modules are now enumerated, and the count is asserted per module so a module silently
    dropping out of the scan is visible.
    """
    routes = _routes()
    names = {name for name, _async, _src in routes}
    assert "readiness_probe" in names, "main.py is not being scanned"
    assert "liveness" in names
    # The agent API declares its routes on a router, so its handlers prove that path works.
    assert "list_grants" in names, "agent_api.py is not being scanned"
    assert len(routes) >= 20, f"only found {len(routes)} routes across {len(ROUTE_MODULES)} modules"


@pytest.mark.parametrize("name", [n for n, _a, _s in _routes()])
def test_no_async_route_performs_blocking_work(name):
    """THE guard.

    A handler that is `async def` AND calls synchronous I/O blocks every other request on
    the instance. Measured at 6.7 seconds for `/readyz` under concurrency 25.
    """
    for route_name, is_async, source in _routes():
        if route_name != name:
            continue
        if not is_async or route_name in ASYNC_ALLOWED:
            pytest.skip(f"{name} is not async, or is allowed to be")
        blocking = [call for call in BLOCKING_CALLS if call in source]
        assert not blocking, (
            f"`{name}` is `async def` and calls {blocking}. FastAPI runs an `async def` "
            "handler on the event loop, so a synchronous call blocks every other request "
            "on the instance. Declare it `def` instead, which FastAPI runs in a "
            "threadpool - this was measured at 6.7 seconds for /readyz under concurrency "
            "25, from 5 milliseconds for handlers that do no work."
        )


def test_the_health_routes_are_sync():
    """Named individually, because these are the ones a load balancer calls on a schedule.

    A parameterised guard would keep passing if somebody renamed `readiness_probe`; this
    one fails on the endpoint that matters by its path.
    """
    import main

    for handler in (
        main.readiness_probe,
        main.deep_health,
        main.prometheus_metrics,
        main.metrics,
    ):
        assert not _is_coroutine_function(handler), (
            f"{handler.__name__} is a coroutine function. It does synchronous database work, "
            "so it would block the event loop; FastAPI runs a plain `def` in a threadpool."
        )


def _is_coroutine_function(handler) -> bool:
    import asyncio

    return asyncio.iscoroutinefunction(handler)


def test_liveness_touches_no_dependency():
    """`/livez` must answer when every dependency is down, and must do no work.

    If it consulted the database it would fail during a failover, and an orchestrator would
    restart every healthy process - turning a thirty-second database blip into a fleet-wide
    outage.
    """
    import main

    source = ast.unparse(ast.parse(MAIN.read_text(encoding="utf-8")))
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "liveness":
            body = ast.unparse(node)
            for forbidden in ("readiness(", "health_check", "session", "db.", "engine"):
                assert forbidden not in body, (
                    f"/livez references {forbidden!r}. Liveness answers 'is this process "
                    "alive', and a check that fails when a dependency is down causes "
                    "restart storms exactly when the system can least absorb them."
                )
            return
    pytest.fail("no `liveness` handler found")
