"""Structural guard for the argument-order bug the delivery routes shipped with.

`require_org_access` takes `(tenant, db, org_id)`. The first version of the delivery
routes called it as `(tenant, org_id, db)` in all four handlers. The effect was not a
crash: `tenant.require_member(session)` failed the membership check and the route
answered **403 for a request that had every right to succeed**. A silently wrong
authorisation answer is worse than an exception, because nothing in the logs looks
unusual.

`py_compile` cannot see this and neither could any test that did not exercise the route.
So it is asserted structurally, over the whole module, which is what makes it not recur
the next time someone adds a handler.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

API = BACKEND / "agent_api.py"


def _calls() -> list[tuple[str, int, list[str]]]:
    tree = ast.parse(API.read_text(encoding="utf-8"))
    found: list[tuple[str, int, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and getattr(sub.func, "id", None) == "require_org_access"
                ):
                    found.append((node.name, sub.lineno, [ast.unparse(a) for a in sub.args]))
    return found


def test_every_require_org_access_call_uses_the_declared_order():
    """`(tenant, db, org_id)`. Four delivery routes had the last two swapped, and the
    only symptom was a 403 on requests that should have succeeded."""
    bad = [(name, line, args) for name, line, args in _calls() if args != ["tenant", "db", "org_id"]]
    assert not bad, (
        "require_org_access is declared (tenant, db, org_id); these calls disagree: "
        + "; ".join(f"{name} at L{line} -> ({', '.join(args)})" for name, line, args in bad)
    )


def test_the_signature_is_still_what_the_callers_assume():
    """So the guard above stays meaningful if the signature ever changes."""
    import inspect

    from router import require_org_access

    parameters = list(inspect.signature(require_org_access).parameters)
    assert parameters == ["tenant", "db", "org_id"], (
        f"require_org_access now takes {parameters}; the guard in this file and every "
        "positional call site must be updated together"
    )


def test_the_delivery_routes_exist():
    """A route that is referenced nowhere and defined nowhere is an easy omission."""
    import main

    paths = {getattr(route, "path", "") for route in main.app.routes}
    for expected in (
        "/api/v1/agent/grants",
        "/api/v1/agent/grants/{grant_id}",
        "/api/v1/agent/deadlines",
        "/api/v1/agent/compliance",
    ):
        assert expected in paths, f"{expected} is not registered"


def test_every_handler_that_resolves_a_tenant_binds_it():
    """A handler that forgets `require_org_access` reads nothing at all, because the
    policy denies every row without a bound tenant - it fails closed, which is correct,
    but it means a forgotten call is a broken endpoint nobody notices until a customer
    reports an empty screen.

    Asserted over the module: every handler taking a TenantContext must call it.
    """
    tree = ast.parse(API.read_text(encoding="utf-8"))
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        source = ast.unparse(node)
        if "Depends(get_tenant_context)" not in source:
            continue
        if "require_org_access" not in source:
            missing.append(node.name)
    assert not missing, (
        "these handlers resolve a tenant but never bind it, so they read nothing: "
        + ", ".join(missing)
    )
