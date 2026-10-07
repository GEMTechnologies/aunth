"""What a token may and may not decide about tenancy.

There are two mechanisms in this service that look like they answer the same
question and do not:

* ``security.resolve_tenant`` / ``security.require_tenant`` read the tenant from
  the **signed token payload**.
* ``tenant_context.TenantContext`` reads it from the **database**, from the
  ``org_members`` row the caller proved by authenticating.

Only the second one runs in the request path. That is the correct arrangement -
membership can be revoked without waiting for a token to expire - but it means
the ``org_id`` claim inside the access token is an *audit record*, not an
authorisation input, and nothing in the code said so. A reader of
``create_access_token`` would reasonably believe otherwise.

These tests pin the arrangement so it cannot be quietly inverted later:

* a token whose ``org_id`` claim names somebody else's organisation still
  cannot reach that organisation;
* a token with no tenant claim cannot reach any organisation;
* but a user who belongs to no organisation can still authenticate and create
  their first one, because failing login at that point would strand them with no
  way to finish onboarding.

Run from ``Auth/backend`` with::

    .venv\\Scripts\\python -m pytest tests\\test_tenant_authority.py -q
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from security import create_access_token, decode_access_token


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient as _TestClient

    import main

    with _TestClient(main.app) as test_client:
        yield test_client


def _register(client, email: str) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "a-strong-password", "full_name": "T"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _login(client, email: str, password: str = "a-strong-password") -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _mint_for(user_id: str, org_id=None) -> str:
    """A correctly signed token with an arbitrary tenant claim.

    Signed with the real secret, because the point is that signature validity is
    not what stops this - the request path must not consult the claim at all.
    """
    return create_access_token(subject=user_id, org_id=org_id)


# ---------------------------------------------------------------------------
# The claim is not authority
# ---------------------------------------------------------------------------

def test_a_forged_tenant_claim_does_not_grant_access_to_that_tenant(client):
    """The central claim.

    If the ``org_id`` claim were consulted, this would succeed. It must not:
    the caller is not a member, so membership is absent from the database and
    the request must be denied regardless of what the token asserts.
    """
    owner = _register(client, "owner@example.com")
    victim = _register(client, "victim@example.com")

    owner_token = _login(client, "owner@example.com")
    org_id = client.post(
        "/api/v1/orgs",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"name": "Owner Org"},
    ).json()["id"]

    # Same subject, same valid signature - only the tenant claim differs.
    forged = _mint_for(victim["id"], org_id=org_id)

    response = client.get(
        f"/api/v1/orgs/{org_id}/members",
        headers={"Authorization": f"Bearer {forged}"},
    )
    assert response.status_code in (401, 403), (
        "a token asserting a tenant the user does not belong to was accepted; "
        f"got {response.status_code} {response.text[:300]}"
    )
    assert victim["id"] not in response.text


def test_the_signed_claim_does_not_match_the_effective_tenant(client):
    """Document the discrepancy rather than leave it to be discovered later.

    The claim says one thing and the effective tenant is another. That is
    expected - the database wins - and asserting it here means a future change
    to either side fails a test instead of quietly changing the model.
    """
    user = _register(client, "mismatch@example.com")
    token = _login(client, "mismatch@example.com")

    claims = decode_access_token(token)
    assert claims["org_id"] is None, (
        "precondition: a user who has created no organisation should have no "
        "tenant claim"
    )

    org_id = client.post(
        "/api/v1/orgs",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "After Login"},
    ).json()["id"]

    # The token still says None; the database now says the user owns an org.
    assert decode_access_token(token)["org_id"] is None
    assert org_id


def test_a_token_with_no_tenant_cannot_name_any_organisation(client):
    owner = _register(client, "owner2@example.com")
    owner_token = _login(client, "owner2@example.com")
    org_id = client.post(
        "/api/v1/orgs",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"name": "Owned"},
    ).json()["id"]

    outsider = _register(client, "outsider@example.com")
    outsider_token = _login(client, "outsider@example.com")

    assert decode_access_token(outsider_token)["org_id"] is None

    response = client.get(
        f"/api/v1/orgs/{org_id}/members",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert response.status_code in (401, 403), response.text
    assert owner["id"] not in response.text


# ---------------------------------------------------------------------------
# "Tenant unknown" denies, but does not strand
# ---------------------------------------------------------------------------

def test_a_user_with_no_organisation_can_still_authenticate(client):
    """Login must not require a tenant.

    A brand-new account has no organisation. If login demanded one, the user
    could never reach the screen where they create it - an account that exists
    but can do nothing. So the token is minted with ``org_id=None``, and that
    token is denied every tenant-scoped operation instead.
    """
    _register(client, "newcomer@example.com")
    token = _login(client, "newcomer@example.com")
    assert decode_access_token(token)["org_id"] is None


def test_onboarding_is_possible_with_a_tenant_less_token(client):
    """The converse of the test above: onboarding must actually work."""
    _register(client, "onboarder@example.com")
    token = _login(client, "onboarder@example.com")

    response = client.post(
        "/api/v1/orgs",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "My First Org"},
    )
    assert response.status_code == 200, (
        f"a user with no organisation could not create their first one: "
        f"{response.status_code} {response.text[:300]}"
    )
    assert response.json()["id"]


def test_membership_removed_after_issuance_takes_effect_immediately(client):
    """The reason the database outranks the token.

    A token is valid for 30 minutes. If that token's ``org_id`` claim were the
    authority, removing a user's membership would not revoke their access for
    up to half an hour. Because the request path re-resolves membership on every
    request, the claim in a still-valid token is irrelevant.
    """
    _register(client, "stale@example.com")
    token = _login(client, "stale@example.com")
    org_id = client.post(
        "/api/v1/orgs",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Revocable"},
    ).json()["id"]

    # Re-authenticate now that the organisation exists, so the token actually
    # asserts it. Without this the token's claim is None, the token is
    # indistinguishable from a tenant-less one, and revoking membership would
    # prove nothing - it would be denied for want of a claim either way.
    token = _login(client, "stale@example.com")
    assert decode_access_token(token)["org_id"] == org_id, (
        "precondition: the fresh token should carry the organisation it "
        "resolved at login time"
    )

    assert client.get(
        f"/api/v1/orgs/{org_id}/members",
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 200

    # Revoke membership directly in the database, as an administrator would.
    import models
    from database import SessionLocal

    db = SessionLocal()
    try:
        db.query(models.OrgMember).filter(
            models.OrgMember.user_id == decode_access_token(token)["sub"],
            models.OrgMember.org_id == org_id,
        ).delete()
        db.commit()
    finally:
        db.close()

    response = client.get(
        f"/api/v1/orgs/{org_id}/members",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code in (401, 403), (
        "a token that correctly names the organisation kept access after the "
        "membership behind that claim was revoked; the token claim is being "
        "trusted over the database, so revoking a member takes effect only "
        "when their token happens to expire"
    )