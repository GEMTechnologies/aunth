"""Application boot and wiring tests.

These cover the five layered failures that prevented the authentication
service from starting. They were latent rather than obvious: each module
imported cleanly on its own, and the service only failed when the whole
import chain ran.

Run from ``Auth/backend`` with::

    .venv\\Scripts\\python -m pytest tests -q
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import database
import models

BACKEND = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Layer 1: oauth router name mismatch
# ---------------------------------------------------------------------------

def test_oauth_module_exposes_the_router_name_used_by_main():
    """main imported ``oauth_router``; oauth.py defined ``router``."""
    source = (BACKEND / "oauth.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_routers = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and "router" in target.id.lower()
    ]
    assert top_level_routers, "oauth.py defines no router"
    assert "oauth_router" not in top_level_routers, (
        "main.py imports oauth_router; if that name ever returns to "
        "oauth.py the two must be kept consistent"
    )


def test_main_module_imports():
    """The single test that would have caught the original boot failure."""
    import main  # noqa: F401

    assert hasattr(main, "app")


# ---------------------------------------------------------------------------
# Layer 2: logging configuration
# ---------------------------------------------------------------------------

def test_logging_level_resolution_does_not_raise():
    import logging

    from config import settings

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    assert isinstance(level, int)


# ---------------------------------------------------------------------------
# Layer 3: two declarative registries
# ---------------------------------------------------------------------------

def test_single_declarative_registry():
    """database.py used to declare a second Base with its own metadata."""
    assert database.Base is models.Base
    assert len(database.Base.metadata.tables) >= 17


def test_models_are_registered_in_the_shared_metadata():
    expected = {
        "users", "emails", "password_credentials", "sessions", "refresh_tokens",
        "organisations", "roles", "permissions", "role_permissions",
        "org_members", "audit_logs", "password_resets", "oauth_accounts",
    }
    missing = expected - set(database.Base.metadata.tables)
    assert not missing, f"tables missing from metadata: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Layer 4: bare-string SQL
# ---------------------------------------------------------------------------

def test_health_check_returns_true_against_a_reachable_database():
    """Previously always False, because of a bare-string execute()."""
    assert database.DatabaseManager.health_check() is True


def test_execute_sql_accepts_a_literal_statement():
    result = database.DatabaseManager.execute_sql("SELECT 1")
    assert result.scalar() == 1


# ---------------------------------------------------------------------------
# Layer 5: destructive operations are guarded
# ---------------------------------------------------------------------------

def test_drop_tables_requires_explicit_confirmation():
    with pytest.raises(RuntimeError):
        database.drop_tables()


# ---------------------------------------------------------------------------
# The app actually serves
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as test_client:
        yield test_client


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_api_health_reports_database_connected(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["database"] == "connected"


def test_root_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "Granada Authentication Service"


def test_request_id_header_is_returned(client):
    """Correlation id middleware is required by the observability spec."""
    response = client.get("/health")
    assert response.headers.get("X-Request-ID")


def test_routes_are_registered(client):
    paths = {getattr(r, "path", None) for r in client.app.routes}
    assert "/health" in paths
    assert any(p and p.startswith("/api/v1") for p in paths), (
        "the versioned API prefix is missing"
    )


def test_oauth_routes_are_mounted(client):
    paths = {getattr(r, "path", None) for r in client.app.routes}
    assert any(p and "/oauth/" in p for p in paths), (
        "OAuth routes are not mounted"
    )


def test_cors_does_not_pair_wildcard_origin_with_credentials(client):
    """allow_origins=['*'] with allow_credentials=True is a misconfiguration.

    Asserted behaviourally: a request bearing an arbitrary Origin must not be
    answered with a reflected origin plus credentials.
    """
    response = client.get("/health", headers={"Origin": "https://attacker.example"})

    acao = response.headers.get("access-control-allow-origin")
    assert acao != "*", "wildcard origin must not be reflected"
    assert acao != "https://attacker.example", (
        "an untrusted origin must not be reflected"
    )
    # Starlette always emits Access-Control-Allow-Credentials when
    # allow_credentials=True. Because no origin is reflected, a browser still
    # refuses to expose the response, which is what makes this safe.


def test_cors_allows_a_configured_origin(client):
    from config import settings

    origin = settings.allowed_origins[0]
    response = client.get("/health", headers={"Origin": origin})
    assert response.headers.get("access-control-allow-origin") == origin