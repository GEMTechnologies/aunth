"""The refresh token must not be reachable from JavaScript in a browser.

The browser client previously kept both tokens in ``localStorage``. Any script
that manages to execute on the origin -- an XSS, a compromised dependency, a
malicious browser extension -- can read them. The access token is short-lived
and re-obtainable. The refresh token is not: it is valid for 30 days, it rotates
on use, and replaying it from another machine revokes the whole session. A
single XSS is therefore permanent credential theft.

``REFRESH_TOKEN_DELIVERY=cookie`` moves the refresh token into an HttpOnly
cookie, which page script cannot read at all, and omits it from the JSON body
so the credential is not merely discouraged but unreachable.

The default remains ``body`` because first-party API and CLI clients have no
cookie jar to rely on; they are not made to fail by this change.

These tests run the real endpoints through the ASGI app rather than calling
handlers, because the property under test -- "is the token in the response
body or not" -- only exists at the HTTP boundary.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def account(db):
    import schemas
    from service import AuthService

    user = AuthService.register_user(
        db,
        schemas.UserCreate(
            email="cookie-user@example.org",
            password="a-strong-password",
            full_name="Cookie User",
        ),
    )
    return user


@pytest.fixture()
def make_client(db, monkeypatch):
    """Build a TestClient for one refresh-token delivery mode.

    The dependency override MUST be undone at teardown. Leaving it installed
    binds every later test in the session to this module's in-memory database,
    which presents as unrelated "Registration failed" failures far from the
    cause. That is how this file passed alone and failed in the full suite the
    first time it was written.
    """
    from config import settings
    from database import get_db
    import main

    app = main.app
    app.dependency_overrides[get_db] = lambda: db

    def _make(delivery: str) -> TestClient:
        monkeypatch.setattr(settings, "refresh_token_delivery", delivery, raising=False)
        return TestClient(app)

    try:
        yield _make
    finally:
        app.dependency_overrides.pop(get_db, None)


def _set_cookie_headers(response) -> list[str]:
    """Every Set-Cookie header, as text.

    httpx.Headers.get_list is the supported accessor; there is no raw_items.
    """
    return [v for v in response.headers.get_list("set-cookie")]


def _cookie_value(set_cookie_headers: list[str], name: str) -> str | None:
    """The value a Set-Cookie header sets, or None if no such header is present."""
    for text in set_cookie_headers:
        if text.startswith(f"{name}="):
            return text.split(";", 1)[0].split("=", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Body mode is the default and must not regress
# ---------------------------------------------------------------------------


def test_body_is_the_default_delivery_mode():
    """Changing the default would silently break every API client."""
    from config import Settings

    assert Settings().refresh_token_delivery == "body"


def test_api_clients_still_receive_the_refresh_token_in_the_body(
    db, account, make_client
):
    client = make_client("body")
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-user@example.org", "password": "a-strong-password"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["refresh_token"]


# ---------------------------------------------------------------------------
# Cookie mode
# ---------------------------------------------------------------------------


def test_the_refresh_token_is_absent_from_the_response_body(db, account, make_client):
    """The whole point: the credential is not handed to page script."""
    client = make_client("cookie")
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-user@example.org", "password": "a-strong-password"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert not body.get("refresh_token"), (
        "refresh_token was returned to the browser in the response body; "
        "cookie delivery must omit it entirely, not null it"
    )


def test_no_credential_value_appears_in_the_cookie_mode_response(
    db, account, make_client
):
    """The key is still present, as null. What must never appear is a value.

    The JSON body keeps the ``refresh_token`` key so that clients written
    against the body contract do not break on a missing field; in cookie mode
    it serialises as null. Asserting the key itself is absent would be a
    stronger claim than the implementation makes, and would be the kind of test
    that passes for the wrong reason.
    """
    client = make_client("cookie")
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-user@example.org", "password": "a-strong-password"},
    )
    assert response.json()["refresh_token"] is None

    issued = _cookie_value(_set_cookie_headers(response), "granada_refresh")
    assert issued, "no cookie was issued, so there is nothing to leak"
    assert issued not in response.text, (
        "the refresh token issued in the cookie also appears in the response body"
    )


def test_login_sets_an_httponly_cookie(db, account, make_client):
    client = make_client("cookie")
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-user@example.org", "password": "a-strong-password"},
    )
    headers = _set_cookie_headers(response)
    raw = "\n".join(headers)
    assert "granada_refresh=" in raw, raw
    assert re.search(r"httponly", raw, re.IGNORECASE), (
        f"cookie is not HttpOnly; page script could read it: {raw}"
    )


def test_the_cookie_is_scoped_to_the_auth_surface(db, account, make_client):
    """A cookie on '/' would ride along on every request to this origin."""
    client = make_client("cookie")
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-user@example.org", "password": "a-strong-password"},
    )
    raw = "\n".join(_set_cookie_headers(response))
    assert re.search(r"path=/api/v1/auth", raw, re.IGNORECASE), raw


def test_refresh_succeeds_from_the_cookie_alone(db, account, make_client):
    """A browser sends no Authorization header on /auth/refresh."""
    client = make_client("cookie")
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-user@example.org", "password": "a-strong-password"},
    )
    cookie_value = _cookie_value(_set_cookie_headers(login), "granada_refresh")
    assert cookie_value

    refreshed = client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"]


def test_the_rotated_refresh_token_replaces_the_cookie(db, account, make_client):
    """Rotation must apply to the cookie, or the old one is left usable."""
    client = make_client("cookie")
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-user@example.org", "password": "a-strong-password"},
    )
    first = _cookie_value(_set_cookie_headers(login), "granada_refresh")

    client.cookies.set("granada_refresh", first)
    refreshed = client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text

    issued = _cookie_value(_set_cookie_headers(refreshed), "granada_refresh")
    assert issued and issued != first, (
        "the rotated refresh token was not written back to the cookie"
    )


def test_the_rotated_response_also_omits_the_token_from_the_body(
    db, account, make_client
):
    """Otherwise the refresh endpoint would reintroduce the leak."""
    client = make_client("cookie")
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-user@example.org", "password": "a-strong-password"},
    )
    client.cookies.set(
        "granada_refresh", _cookie_value(_set_cookie_headers(login), "granada_refresh")
    )
    refreshed = client.post("/api/v1/auth/refresh")
    assert not refreshed.json().get("refresh_token")


def test_a_replayed_refresh_cookie_is_refused(db, account, make_client):
    """Replay detection must still work when the token came from a cookie."""
    client = make_client("cookie")
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-user@example.org", "password": "a-strong-password"},
    )
    stolen = _cookie_value(_set_cookie_headers(login), "granada_refresh")

    client.cookies.set("granada_refresh", stolen)
    assert client.post("/api/v1/auth/refresh").status_code == 200

    client.cookies.set("granada_refresh", stolen)
    replay = client.post("/api/v1/auth/refresh")
    assert replay.status_code == 401, (
        "a replayed refresh token was accepted; reuse detection is bypassed "
        "when the credential arrives by cookie"
    )


def test_logout_clears_the_cookie(db, account, make_client):
    """Without this the browser keeps replaying a dead credential."""
    client = make_client("cookie")
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-user@example.org", "password": "a-strong-password"},
    )
    client.cookies.set(
        "granada_refresh", _cookie_value(_set_cookie_headers(login), "granada_refresh")
    )

    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 204, response.text

    cleared = "\n".join(_set_cookie_headers(response))
    assert "granada_refresh=" in cleared, (
        f"logout did not clear the refresh cookie: {cleared}"
    )
    assert re.search(r"Max-Age=0|Expires=Thu, 01 Jan 1970", cleared, re.IGNORECASE), (
        f"the cookie was overwritten but not expired, so the browser keeps it: {cleared}"
    )


def test_logout_without_any_credential_still_succeeds(db, account, make_client):
    """A logout that 500s leaves the UI stuck in a logged-in-looking state."""
    client = make_client("cookie")
    assert client.post("/api/v1/auth/logout").status_code == 204


# ---------------------------------------------------------------------------
# Misconfiguration must fail loudly, not silently
# ---------------------------------------------------------------------------


def test_samesite_none_without_secure_is_rejected_at_startup():
    """Browsers drop such a cookie, so every refresh fails 30 minutes later."""
    from pydantic import ValidationError

    from config import Settings

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            app_env="test",
            refresh_token_delivery="cookie",
            refresh_token_cookie_samesite="none",
            refresh_token_cookie_secure=False,
        )
    assert "REFRESH_TOKEN_COOKIE_SECURE" in str(excinfo.value)


def test_an_unknown_delivery_mode_is_rejected():
    from pydantic import ValidationError

    from config import Settings

    with pytest.raises(ValidationError):
        Settings(app_env="test", refresh_token_delivery="cookie-ish")


def test_read_refresh_token_prefers_the_cookie_over_a_stale_header():
    """A leftover token in JS must not override the rotated cookie."""
    from security import read_refresh_token

    cookies = {"granada_refresh": "from-cookie"}
    assert read_refresh_token(cookies, "Bearer from-header") == "from-cookie"


def test_read_refresh_token_falls_back_to_the_header_for_api_clients():
    from security import read_refresh_token

    assert read_refresh_token({}, "Bearer from-header") == "from-header"


def test_read_refresh_token_returns_none_when_nothing_is_present():
    from security import read_refresh_token

    assert read_refresh_token({}) is None
    assert read_refresh_token({}, "not-a-bearer-header") is None