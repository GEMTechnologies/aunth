"""End-to-end authentication and tenancy behaviour.

These exercise the real services against a real database. They are the tests
that would have caught the schema-vs-service mismatches, which no amount of
importing or type checking could: the code referenced columns that did not
exist, and the failures only appear on the INSERT.

An isolated in-memory SQLite database is used so the tests never touch the
developer's PostgreSQL data.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
from security import decode_access_token, hash_token


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


def _register(db, email="founder@example.org", password="a-strong-password"):
    import schemas
    from service import AuthService

    return AuthService.register_user(
        db,
        schemas.UserCreate(email=email, password=password, full_name="Test User"),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_user_persists_user_email_and_credential(db):
    user = _register(db)
    assert user.id

    email = db.query(models.Email).filter_by(user_id=user.id).one()
    assert email.email == "founder@example.org"
    assert email.is_primary is True
    assert email.is_verified is False

    cred = db.query(models.PasswordCredential).filter_by(user_id=user.id).one()
    assert cred.password_hash.startswith("$argon2id$")
    assert "a-strong-password" not in cred.password_hash


def test_register_duplicate_email_is_rejected(db):
    _register(db)
    with pytest.raises(HTTPException) as excinfo:
        _register(db)
    assert excinfo.value.status_code == 400


def test_authenticate_user_accepts_correct_password(db):
    from service import AuthService

    _register(db)
    user = AuthService.authenticate_user(db, "founder@example.org", "a-strong-password")
    assert user.id


def test_authenticate_user_rejects_wrong_password(db):
    from service import AuthService

    _register(db)
    with pytest.raises(HTTPException) as excinfo:
        AuthService.authenticate_user(db, "founder@example.org", "wrong-password")
    assert excinfo.value.status_code == 401


def test_authenticate_unknown_email_is_rejected(db):
    from service import AuthService

    with pytest.raises(HTTPException) as excinfo:
        AuthService.authenticate_user(db, "nobody@example.org", "a-strong-password")
    assert excinfo.value.status_code == 401


# ---------------------------------------------------------------------------
# Sessions and tokens
# ---------------------------------------------------------------------------

def test_create_session_populates_real_columns(db):
    """The service wrote ip_created; the column is ip_first."""
    from service import SessionService

    user = _register(db)
    session = SessionService.create_session(
        db, user, ip_address="203.0.113.7", user_agent="pytest"
    )
    assert session.ip_first == "203.0.113.7"
    assert session.ip_last == "203.0.113.7"
    assert session.user_agent == "pytest"


def test_issue_tokens_stores_refresh_hash_in_its_own_table(db):
    """Refresh tokens belong in refresh_tokens, not on the session row."""
    from service import SessionService

    user = _register(db)
    session = SessionService.create_session(db, user)
    pair = SessionService.issue_tokens(db, user, session)

    stored = db.query(models.RefreshToken).filter_by(session_id=session.id).one()
    assert stored.token_hash == hash_token(pair.refresh_token)
    assert stored.revoked_at is None
    assert pair.refresh_token not in stored.token_hash


def test_access_token_carries_tenant_claims(db):
    from service import OrganizationService, SessionService

    user = _register(db)
    org = OrganizationService.create_organization(db, "Test Org", user)
    session = SessionService.create_session(db, user)

    pair = SessionService.issue_tokens(db, user, session)
    claims = decode_access_token(pair.access_token)

    assert claims["org_id"] == org.id
    assert claims["sub"] == str(user.id)
    assert claims["sid"] == str(session.id)
    assert claims["type"] == "access"
    assert claims["jti"]


def test_token_is_unscoped_when_user_has_no_organisation(db):
    """Tenant unknown must not be silently defaulted to some org."""
    from service import SessionService, resolve_tenant_for_user

    user = _register(db)
    tenant = resolve_tenant_for_user(db, user.id)
    assert tenant.org_id is None
    assert tenant.is_resolved is False

    session = SessionService.create_session(db, user)
    pair = SessionService.issue_tokens(db, user, session)
    claims = decode_access_token(pair.access_token)
    assert claims.get("org_id") is None


# ---------------------------------------------------------------------------
# Refresh rotation and replay detection
# ---------------------------------------------------------------------------

def test_rotate_refresh_token_issues_a_new_pair(db):
    from service import SessionService

    user = _register(db)
    session = SessionService.create_session(db, user)
    first = SessionService.issue_tokens(db, user, session)

    rotated = SessionService.rotate_refresh_token(db, first.refresh_token)
    assert rotated.refresh_token != first.refresh_token
    assert rotated.access_token


def test_rotated_token_cannot_be_reused(db):
    from service import SessionService

    user = _register(db)
    session = SessionService.create_session(db, user)
    first = SessionService.issue_tokens(db, user, session)
    SessionService.rotate_refresh_token(db, first.refresh_token)

    with pytest.raises(HTTPException) as excinfo:
        SessionService.rotate_refresh_token(db, first.refresh_token)
    assert excinfo.value.status_code == 401


def test_reuse_detection_revokes_the_whole_session(db):
    """Replaying a stolen token must invalidate the attacker's session too."""
    from service import SessionService

    user = _register(db)
    session = SessionService.create_session(db, user)
    first = SessionService.issue_tokens(db, user, session)
    second = SessionService.rotate_refresh_token(db, first.refresh_token)

    with pytest.raises(HTTPException):
        SessionService.rotate_refresh_token(db, first.refresh_token)

    db.expire_all()
    reloaded = db.query(models.Session).filter_by(id=session.id).one()
    assert reloaded.revoked_at is not None

    with pytest.raises(HTTPException):
        SessionService.rotate_refresh_token(db, second.refresh_token)


def test_unknown_refresh_token_is_rejected(db):
    from service import SessionService

    with pytest.raises(HTTPException) as excinfo:
        SessionService.rotate_refresh_token(db, "not-a-real-token")
    assert excinfo.value.status_code == 401


def test_revoked_refresh_token_is_rejected(db):
    from service import SessionService

    user = _register(db)
    session = SessionService.create_session(db, user)
    pair = SessionService.issue_tokens(db, user, session)
    SessionService.revoke_refresh_token(db, pair.refresh_token)

    with pytest.raises(HTTPException) as excinfo:
        SessionService.rotate_refresh_token(db, pair.refresh_token)
    assert excinfo.value.status_code == 401


def test_expired_refresh_token_is_rejected(db):
    from datetime import timedelta, datetime, timezone

    from service import SessionService

    user = _register(db)
    session = SessionService.create_session(db, user)
    pair = SessionService.issue_tokens(db, user, session)

    stored = db.query(models.RefreshToken).filter_by(
        token_hash=hash_token(pair.refresh_token)
    ).one()
    stored.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    with pytest.raises(HTTPException) as excinfo:
        SessionService.rotate_refresh_token(db, pair.refresh_token)
    assert excinfo.value.status_code == 401


# ---------------------------------------------------------------------------
# Organisation membership
# ---------------------------------------------------------------------------

def test_create_organization_creates_owner_membership(db):
    """OrgMember takes org_id and role_id; the service passed org/role."""
    from service import OrganizationService

    user = _register(db)
    org = OrganizationService.create_organization(db, "Childcare Trust", user)

    member = db.query(models.OrgMember).filter_by(
        org_id=org.id, user_id=user.id
    ).one()
    role = db.query(models.Role).filter_by(id=member.role_id).one()
    assert role.key == "owner"


def test_create_organization_slugifies_the_name(db):
    from service import OrganizationService

    user = _register(db)
    org = OrganizationService.create_organization(db, "Water & Sanitation NGO!", user)
    assert org.slug == "water-sanitation-ngo"
    assert " " not in org.slug


def test_duplicate_organisation_slugs_are_disambiguated(db):
    from service import OrganizationService

    first_user = _register(db, email="a@example.org")
    second_user = _register(db, email="b@example.org")

    org_a = OrganizationService.create_organization(db, "Same Name", first_user)
    org_b = OrganizationService.create_organization(db, "Same Name", second_user)
    assert org_a.slug != org_b.slug


def test_add_member_resolves_role_key(db):
    from service import OrganizationService

    owner = _register(db, email="owner@example.org")
    member = _register(db, email="staff@example.org")
    org = OrganizationService.create_organization(db, "Grants Agency", owner)

    OrganizationService.add_member(db, member.id, org.id, "finance")
    tenant = __import__("service").resolve_tenant_for_user(db, member.id)
    assert tenant.org_id == org.id
    assert "finance" in tenant.roles


def test_resolve_tenant_prefers_the_owned_organisation(db):
    from service import OrganizationService, resolve_tenant_for_user

    owner = _register(db, email="owner@example.org")
    org = OrganizationService.create_organization(db, "Owned", owner)
    assert resolve_tenant_for_user(db, owner.id).org_id == org.id


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------

def test_membership_never_crosses_organisations(db):
    from service import OrganizationService, resolve_tenant_for_user

    alice = _register(db, email="alice@example.org")
    bob = _register(db, email="bob@example.org")

    org_a = OrganizationService.create_organization(db, "Org A", alice)
    org_b = OrganizationService.create_organization(db, "Org B", bob)
    OrganizationService.add_member(db, bob.id, org_a.id, "advisor")

    alice_tenant = resolve_tenant_for_user(db, alice.id)
    bob_tenant = resolve_tenant_for_user(db, bob.id)

    assert alice_tenant.org_id == org_a.id
    assert bob_tenant.org_id == org_b.id
    assert alice_tenant.org_id != bob_tenant.org_id


def test_token_from_one_org_denies_another_org(db):
    from security import TenantScopeError, resolve_tenant, require_tenant

    from service import OrganizationService, SessionService

    alice = _register(db, email="alice@example.org")
    bob = _register(db, email="bob@example.org")
    org_a = OrganizationService.create_organization(db, "Org A", alice)
    org_b = OrganizationService.create_organization(db, "Org B", bob)

    session = SessionService.create_session(db, alice)
    pair = SessionService.issue_tokens(db, alice, session)
    claims = decode_access_token(pair.access_token)

    assert resolve_tenant(claims) == org_a.id

    # Own organisation is accepted.
    assert require_tenant(claims, org_a.id) == org_a.id

    # A different organisation is denied, not silently served.
    with pytest.raises(TenantScopeError):
        require_tenant(claims, org_b.id)

    # A user who belongs to no organisation gets a token carrying no tenant.
    # A tenant-scoped call with it is denied, never served under a default org.
    solo = _register(db, email="solo@example.org")
    solo_claims = decode_access_token(
        SessionService.issue_tokens(
            db, solo, SessionService.create_session(db, solo)
        ).access_token
    )
    assert solo_claims.get("org_id") is None
    with pytest.raises(TenantScopeError):
        require_tenant(solo_claims, org_a.id)


# ---------------------------------------------------------------------------
# Audit persistence
# ---------------------------------------------------------------------------

def test_audit_record_persists_with_required_ip(db):
    """Every AuditLog INSERT previously omitted the NOT NULL ip column."""
    from service import AuditService

    user = _register(db)
    AuditService.record(
        db,
        event="test.event",
        user_id=user.id,
        payload={"note": "hello"},
        ip_address="198.51.100.4",
    )
    db.commit()

    stored = db.query(models.AuditLog).filter_by(event="test.event").one()
    assert stored.ip == "198.51.100.4"
    assert stored.payload_json == {"note": "hello"}


def test_audit_record_survives_without_an_ip(db):
    from service import AuditService

    user = _register(db)
    AuditService.record(db, event="test.no_ip", user_id=user.id)
    db.commit()

    stored = db.query(models.AuditLog).filter_by(event="test.no_ip").one()
    assert stored.ip == "unknown"


def test_direct_audit_construction_no_longer_fails(db):
    """The ten legacy call sites must be able to commit."""
    user = _register(db)
    db.add(models.AuditLog(user_id=user.id, event="legacy.path"))
    db.commit()

    assert db.query(models.AuditLog).filter_by(event="legacy.path").one()


def test_audit_redacts_credentials(db):
    from service import AuditService

    user = _register(db)
    AuditService.record(
        db,
        event="test.redaction",
        user_id=user.id,
        payload={
            "email": "user@example.org",
            "password": "hunter2",
            "access_token": "eyJhbGciOi",
            "nested_ok": True,
        },
    )
    db.commit()

    stored = db.query(models.AuditLog).filter_by(event="test.redaction").one()
    assert stored.payload_json["password"] == "[redacted]"
    assert stored.payload_json["access_token"] == "[redacted]"
    assert stored.payload_json["email"] == "user@example.org"
    assert stored.payload_json["nested_ok"] is True
    assert "hunter2" not in str(stored.payload_json)


def test_audit_redaction_is_case_insensitive(db):
    from service import AuditService

    assert AuditService.redact({"Password": "x"})["Password"] == "[redacted]"
    assert AuditService.redact({"ACCESS_TOKEN": "x"})["ACCESS_TOKEN"] == "[redacted]"
    assert AuditService.redact(None) == {}


def test_password_reset_audit_persists(db):
    from service import PasswordResetService

    user = _register(db, email="reset@example.org")
    PasswordResetService.request_password_reset(db, "reset@example.org")

    stored = db.query(models.AuditLog).filter_by(
        event="password.reset_requested"
    ).one()
    assert stored.user_id == user.id
    assert stored.ip == "unknown"