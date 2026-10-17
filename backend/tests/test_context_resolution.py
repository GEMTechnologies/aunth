"""Where a user lands after signing in - the gate the whole product sits behind.

THE DEFECT THIS PINS. `resolve_landing_context` raised `HTTPException(500, "User has no available
contexts")` for any user with no context row, under a comment that read "should not happen for
registered users". It happened for EVERY registered user: `user_contexts` was empty while the real
membership lived in `org_members` and `organisations.owner_user_id`. Sign-up worked, sign-in worked,
and the product was unreachable - the client rendered "Something went wrong.

Two separate faults, both covered here:
  1. the wrong source of truth (an unpopulated table instead of membership), and
  2. raising 500 for a legitimate state (a user who has not created an organisation yet).

The third was a contract mismatch: the response model demanded a required `redirect_url` while the
service returns `action`/`url`, so FastAPI's own response validation was a second route to the same
500 - and the client switches on `action`.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import make_sqlite_db  # noqa: E402

import models  # noqa: E402
import schemas  # noqa: E402
from context_service import ContextService  # noqa: E402


@pytest.fixture
def db(tmp_path):
    engine, session = make_sqlite_db(tmp_path, "context.db")
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def user(db, name="Owner"):
    row = models.User(id=str(uuid.uuid4()), display_name=name)
    db.add(row)
    db.commit()
    return row


def org(db, owner, *, name="Granada Test Organisation"):
    row = models.Organisation(
        id=str(uuid.uuid4()), name=name,
        slug=f"org-{uuid.uuid4().hex[:8]}", owner_user_id=owner.id,
    )
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# A user with no organisation is ONBOARDING, not an error
# ---------------------------------------------------------------------------
def test_a_new_user_gets_onboarding_and_not_a_500(db):
    """The exact state every account is in immediately after signing up."""
    fresh = user(db, "Brand New")

    result = ContextService.resolve_landing_context(db, fresh.id, "granadac.com", None)

    assert result["action"] == "onboarding"
    assert result["contexts"] == []
    assert result.get("url") is None


def test_the_onboarding_response_validates_against_the_declared_contract(db):
    """FastAPI validates responses against the model, so a mismatch is a 500 even when the service
    is right. The model used to require `redirect_url` and the service never sent it."""
    fresh = user(db, "Brand New")
    result = ContextService.resolve_landing_context(db, fresh.id, "granadac.com", None)
    model = schemas.ContextResolutionResponse(**result)
    assert model.action == "onboarding"


# ---------------------------------------------------------------------------
# Membership is the source of truth
# ---------------------------------------------------------------------------
def test_an_organisations_owner_resolves_to_that_organisation(db):
    """`user_contexts` is empty on production while the owner is a real member. Reading the
    unpopulated table was the reason the owner could not get in to their own workspace."""
    owner = user(db, "Owner")
    organisation = org(db, owner)

    result = ContextService.resolve_landing_context(db, owner.id, "granadac.com", None)

    assert result["action"] == "redirect"
    assert result["context"]["org_id"] == organisation.id
    assert result["context"]["role"] == "owner"
    assert organisation.slug in result["url"]


def test_a_member_resolves_to_a_context_but_not_as_owner(db):
    member = user(db, "Member")
    owner = user(db, "Owner")
    organisation = org(db, owner)
    # OrgMember has a COMPOSITE primary key (org_id, user_id) and a required role_id - a fixture
    # that invents an `id` column is how a test stops resembling what production writes.
    role = models.Role(id=str(uuid.uuid4()), org_id=organisation.id, key="member", name="Member")
    db.add(role)
    db.commit()
    db.add(
        models.OrgMember(
            org_id=organisation.id, user_id=member.id, role_id=role.id,
            joined_at=datetime.now(timezone.utc),
        )
    )
    db.commit()

    result = ContextService.resolve_landing_context(db, member.id, "granadac.com", None)
    # A single context redirects; the role is what distinguishes owner from member.
    assert result["action"] == "redirect"
    assert result["context"]["role"] != "owner"


def test_the_owner_context_carries_the_slug_the_client_routes_on(db):
    """`_url_for_context` falls back to the org id when the slug is missing, so a missing slug is
    invisible in the URL - but it is the difference between fetching the organisation and not."""
    owner = user(db, "Owner")
    organisation = org(db, owner)

    meta = ContextService.get_user_contexts(db, owner.id)
    assert meta["contexts"][0]["org_slug"] == organisation.slug
    assert meta["contexts"][0]["org_name"] == organisation.name


def test_get_user_contexts_reports_the_membership(db):
    owner = user(db, "Owner")
    organisation = org(db, owner)

    meta = ContextService.get_user_contexts(db, owner.id)
    assert len(meta["contexts"]) == 1
    assert meta["contexts"][0]["org_id"] == organisation.id


def test_a_user_who_does_not_exist_is_still_a_404(db):
    """404 means "no such user", which is genuinely different from "user has no workspace"."""
    with pytest.raises(HTTPException) as excinfo:
        ContextService.resolve_landing_context(db, str(uuid.uuid4()), "granadac.com", None)
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
def test_the_model_carries_the_action_the_client_switches_on():
    """`ContextRouter` reads `action`, `url`, `context`, `contexts` and `product`; the model used to
    declare none of them."""
    model = schemas.ContextResolutionResponse(
        action="show_picker",
        contexts=[{"type": "org", "org_id": "x"}],
    )
    payload = model.model_dump()
    assert payload["action"] == "show_picker"
    assert payload["contexts"] == [{"type": "org", "org_id": "x"}]


def test_an_older_client_reading_redirect_url_still_validates():
    model = schemas.ContextResolutionResponse(action="redirect", redirect_url="https://x/y")
    assert model.redirect_url == "https://x/y"


def test_a_redirect_response_carries_a_url():
    model = schemas.ContextResolutionResponse(action="redirect", url="https://granadac.com/ngos/acme/dashboard")
    assert model.url.endswith("/dashboard")
