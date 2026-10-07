"""Router integrity: no shadowed routes, no names that do not exist.

Two defects are covered, both of which were live in this service and both of
which are invisible from any single module in isolation.

1. **Duplicate route registration.** ``main.py`` mounts both ``router`` and
   ``oauth.router`` under ``/api/v1``. Anything declared in both is registered
   twice, and Starlette answers from the *first* match - so a broken copy in
   ``router.py`` silently shadows a working copy in ``oauth.py``, with no
   warning. That is exactly what happened: ``router.py`` carried its own
   ``/auth/oauth/{provider}/authorize`` and ``.../callback`` handlers, and
   because ``router`` is included first, oauth.py's correct implementations were
   unreachable.

2. **Names that resolve to nothing at runtime.** The shadowing handlers called a
   bare ``oauth_service``; ``change_password`` called bare ``verify_password``
   and ``hash_password``. This module imports the *modules*, not those names, so
   each was a NameError at request time - and because every handler wraps its
   body in ``except Exception: raise HTTPException(500)``, all three presented as
   an uninformative server error rather than as a bug.

The second check walks the standard library's ``symtable`` rather than grepping
for strings, so it reports names a function *reads* but the module never binds.
It is intentionally narrow: unresolved globals only. It says nothing about
attribute typos or about types.
"""

from __future__ import annotations

import builtins
import pathlib
import symtable
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

BACKEND = pathlib.Path(__file__).resolve().parents[1]
ROUTER_MODULES = ("router.py", "oauth.py", "main.py")


def _unbound_globals(source: str, filename: str) -> set[str]:
    """Names read as globals that the module never binds and that are not builtins."""
    module = symtable.symtable(source, filename, "exec")
    module_names = set(module.get_identifiers())

    unbound: set[str] = set()

    def walk(table: symtable.SymbolTable) -> None:
        for symbol in table.get_symbols():
            # A global reference is one the scope does not resolve itself, so
            # Python looks it up in the module - and then in builtins.
            if symbol.is_global() and symbol.is_referenced():
                name = symbol.get_name()
                if name not in module_names and not hasattr(builtins, name):
                    unbound.add(f"{name} (in {table.get_name()})")
        for child in table.get_children():
            walk(child)

    for child in module.get_children():
        walk(child)
    return unbound


@pytest.mark.parametrize("module_name", ROUTER_MODULES)
def test_module_binds_every_name_its_handlers_use(module_name: str) -> None:
    """No handler may reference a global the module does not define."""
    path = BACKEND / module_name
    source = path.read_text(encoding="utf-8")

    unbound = _unbound_globals(source, module_name)

    assert not unbound, (
        f"{module_name} references names that do not exist at module level. "
        f"Each is a NameError the first time the enclosing handler runs, "
        f"reported as a 500 by the handler's broad except clause: {sorted(unbound)}"
    )


def test_the_unbound_global_check_would_have_caught_the_defect() -> None:
    """Guard the guard.

    A checker that finds nothing because it walks the wrong scopes is worse than
    no checker, so this asserts the walk actually reports a name the module does
    not bind.
    """
    source = (
        "import os\n"
        "def handler():\n"
        "    return not_a_real_name()\n"
    )
    unbound = _unbound_globals(source, "synthetic.py")
    assert any("not_a_real_name" in item for item in unbound), (
        "the symtable walk stopped detecting undefined globals; every test in "
        "this module that relies on it is now vacuous"
    )


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as test_client:
        yield test_client


def _registered_routes(app) -> list[tuple[str, str, str]]:
    """(method, path, endpoint module.function) for every registered route."""
    from fastapi.routing import APIRoute

    found = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        endpoint = route.endpoint
        where = f"{getattr(endpoint, '__module__', '?')}.{getattr(endpoint, '__name__', '?')}"
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            found.append((method, route.path, where))
    return found


def test_no_path_is_registered_twice(client) -> None:
    """The shadowing defect itself.

    Two routers are mounted under the same prefix. Any path they share is
    registered twice and only the first copy is ever reachable, so the second is
    untested dead weight that can silently take over the request.
    """
    seen: dict[tuple[str, str], str] = {}
    duplicates: list[str] = []

    for method, path, where in _registered_routes(client.app):
        key = (method, path)
        if key in seen:
            duplicates.append(
                f"{method} {path}: {seen[key]} is shadowed by {where}"
            )
        else:
            seen[key] = where

    assert not duplicates, (
        "these routes are registered more than once; the first copy wins every "
        "match, so the second is unreachable:\n  " + "\n  ".join(duplicates)
    )


def test_oauth_callback_is_owned_by_the_oauth_module(client) -> None:
    """The callback must issue a one-time code, not return tokens in a body.

    ``router.py`` used to register its own callback that returned access and
    refresh tokens in JSON from a GET, on a URL the browser reached by redirect.
    That puts credentials in the address bar, in history, and in any proxy that
    logs query strings - which is what the one-time-code exchange exists to
    avoid. Pinned so the copy cannot be reintroduced.
    """
    owners = {
        where
        for method, path, where in _registered_routes(client.app)
        if path.endswith("/oauth/{provider}/callback")
    }
    assert owners == {"oauth.oauth_callback"}, (
        "the OAuth callback must be served by oauth.oauth_callback; "
        f"currently served by {sorted(owners)}"
    )


def test_oauth_authorize_is_owned_by_the_oauth_module(client) -> None:
    owners = {
        where
        for method, path, where in _registered_routes(client.app)
        if path.endswith("/oauth/{provider}/authorize")
    }
    assert owners == {"oauth.initiate_oauth"}, (
        "the OAuth authorize route must be served by oauth.initiate_oauth; "
        f"currently served by {sorted(owners)}"
    )


def test_the_one_time_code_exchange_route_exists(client) -> None:
    """Without this, deleting the duplicated callback would lose the flow.

    The browser must receive ``?code=`` only, and trade it for tokens out of
    band. If the exchange endpoint disappears the design silently reverts to
    putting credentials in the redirect URL.
    """
    paths = {path for _, path, _ in _registered_routes(client.app)}
    assert "/api/v1/auth/oauth/exchange" in paths, (
        "the one-time code exchange endpoint is missing; the OAuth flow would "
        "have no way to obtain tokens without putting them in the redirect URL"
    )