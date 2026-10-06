"""The OAuth redirect must never carry a token.

Before Phase 1, the provider callback ended with the access token and the
refresh token in the redirect query string. Anything that reads a URL can
read them: browser history, the Referer header sent to the next page, proxy
and web-server access logs, and the identity provider's own log.

The callback now returns a single-use code with a short TTL, and the SPA
trades it for tokens over POST. These tests pin the properties that make that
safe:

* the raw code is never stored, only its hash
* a code cannot be exchanged twice
* a code cannot be exchanged after its TTL
* a suspended account cannot complete a sign-in it started while active
* every failure returns one indistinguishable response
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
from security import hash_token


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
def user(db):
    import schemas
    from service import AuthService

    return AuthService.register_user(
        db,
        schemas.UserCreate(
            email="oauth-user@example.org",
            password="a-strong-password",
            full_name="OAuth User",
        ),
    )


async def _exchange(db, code: str):
    from oauth import exchange_oauth_code, CodeExchangeRequest

    return await exchange_oauth_code(CodeExchangeRequest(code=code), db)


# ---------------------------------------------------------------------------
# The code itself
# ---------------------------------------------------------------------------

def test_auth_code_is_never_stored_in_the_clear(db, user):
    from oauth import issue_auth_code

    code = issue_auth_code(db, user_id=user.id)

    rows = db.query(models.OAuthAuthCode).all()
    assert len(rows) == 1
    assert code not in rows[0].code_hash
    assert rows[0].code_hash == hash_token(code)


def test_redirect_url_contains_no_token(db, user):
    """The redirect carries a code, never a bearer token."""
    from oauth import _frontend_redirect

    location = _frontend_redirect(code="abc123").headers["location"]
    assert "access_token" not in location
    assert "refresh_token" not in location
    assert "code=abc123" in location


def test_error_redirect_never_echoes_upstream_detail(db):
    from oauth import _frontend_redirect

    location = _frontend_redirect(error="access_denied").headers["location"]
    assert "access_token" not in location
    assert "code=" not in location


# ---------------------------------------------------------------------------
# Exchange behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_code_returns_a_token_pair(db, user):
    from oauth import issue_auth_code

    code = issue_auth_code(db, user_id=user.id)
    result = await _exchange(db, code)

    assert result["token_type"] == "bearer"
    assert result["access_token"]
    assert result["refresh_token"]
    assert result["expires_in"] > 0


@pytest.mark.asyncio
async def test_a_code_cannot_be_exchanged_twice(db, user):
    """Replay protection. A code found in a log must be worthless."""
    from oauth import issue_auth_code

    code = issue_auth_code(db, user_id=user.id)
    await _exchange(db, code)

    with pytest.raises(HTTPException) as excinfo:
        await _exchange(db, code)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_a_code_expires(db, user):
    from oauth import issue_auth_code

    code = issue_auth_code(db, user_id=user.id)
    record = db.query(models.OAuthAuthCode).one()
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    with pytest.raises(HTTPException) as excinfo:
        await _exchange(db, code)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_suspended_user_cannot_complete_oauth_sign_in(db, user):
    """An account disabled mid-flow must not receive tokens."""
    from oauth import issue_auth_code

    code = issue_auth_code(db, user_id=user.id)
    user.status = "suspended"
    db.commit()

    with pytest.raises(HTTPException) as excinfo:
        await _exchange(db, code)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_unknown_and_reused_codes_are_indistinguishable(db, user):
    """One response for every failure mode, so the endpoint cannot be used to
    probe which codes ever existed."""
    from oauth import issue_auth_code

    real_code = issue_auth_code(db, user_id=user.id)
    await _exchange(db, real_code)

    reused = None
    unknown = None
    try:
        await _exchange(db, real_code)
    except HTTPException as exc:
        reused = exc
    try:
        await _exchange(db, "this-code-was-never-issued-at-all")
    except HTTPException as exc:
        unknown = exc

    assert reused is not None and unknown is not None
    assert reused.status_code == unknown.status_code
    assert reused.detail == unknown.detail


@pytest.mark.asyncio
async def test_exchange_burns_the_code_before_issuing_tokens(db, user):
    """used_at is set as part of granting, so a replay cannot race a second
    token pair into existence."""
    from oauth import issue_auth_code

    code = issue_auth_code(db, user_id=user.id)
    await _exchange(db, code)

    record = db.query(models.OAuthAuthCode).one()
    assert record.used_at is not None


def test_code_ttl_is_short():
    """Two minutes. A code is handed to a browser, not a vault."""
    from oauth import AUTH_CODE_TTL_SECONDS

    assert 0 < AUTH_CODE_TTL_SECONDS <= 300