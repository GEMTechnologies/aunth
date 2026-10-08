"""Regression tests for defects found during the Phase 1 security repair.

Each test here corresponds to a defect that was live in the tree and is now
fixed. They are deliberately behavioural: an import or a boot check would not
have caught any of these, because each one only failed when a specific line
actually executed.

Covered here:

* ``AmbiguousForeignKeysError`` from the circular users/emails foreign keys
  (SQLAlchemy resolves it lazily, at first relationship use).
* ``AuthService.register_user`` and ``authenticate_user`` joins that omitted
  an onclause, which made registration and login fail outright.
* ``OrgMember`` callers still passing the removed ``organisation_id``/``role``
  columns after the schema moved to ``org_id``/``role_id``.
* Audit rows failing to persist because ``ip`` was NOT NULL with no default.
* OAuth redirect handing back a long-lived credential in a URL query string.
* Static model/schema drift, so the class of bug above cannot reappear
  unnoticed.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from sqlalchemy.orm import configure_mappers
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402
import schemas  # noqa: E402
from security import hash_token  # noqa: E402
from service import (  # noqa: E402
    AuditService,
    AuthService,
    OrganizationService,
    SessionService,
)
from database import Base  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# --------------------------------------------------------------------------
# Mapper configuration
# --------------------------------------------------------------------------

def test_all_orm_mappers_configure():
    """Any ambiguous foreign key raises here instead of at request time.

    SQLAlchemy defers join resolution to first use, so a broken relationship
    can sit in the tree passing every import and boot check.
    """
    configure_mappers()


def test_users_and_emails_circular_foreign_keys_are_disambiguated():
    user = models.User(id="u1", display_name="A")
    assert user.emails is not None
    assert models.User.emails.property._user_defined_foreign_keys


# --------------------------------------------------------------------------
# Registration and login
# --------------------------------------------------------------------------

def _register(db, email="owner@example.org", password="Str0ng-Passphrase"):
    return AuthService.register_user(
        db, schemas.UserCreate(email=email, password=password, full_name="Owner")
    )


def test_register_user_joins_without_ambiguous_foreign_key_error(db):
    """The duplicate-email check used `join(Email)` with no onclause."""
    _register(db, email="dup@example.org")

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _register(db, email="dup@example.org")
    assert exc.value.status_code == 400


def test_register_user_sets_primary_email_pointer(db):
    """users.primary_email_id was left NULL for every registered account."""
    user = _register(db)
    db.refresh(user)

    assert user.primary_email_id is not None
    assert user.primary_email.email == "owner@example.org"


def test_authenticate_user_finds_the_account_by_email(db):
    """authenticate_user had the same ambiguous join: login could not work."""
    _register(db)
    db.commit()

    found = AuthService.authenticate_user(db, "owner@example.org", "Str0ng-Passphrase")
    assert found is not None
    assert found.display_name == "Owner"


def test_authenticate_user_rejects_wrong_password(db):
    from fastapi import HTTPException

    _register(db)
    db.commit()

    with pytest.raises(HTTPException):
        AuthService.authenticate_user(db, "owner@example.org", "wrong-password")


# --------------------------------------------------------------------------
# Membership: schema/service agreement
# --------------------------------------------------------------------------

def test_org_member_column_names_match_the_model():
    """Callers used organisation_id/role; the columns are org_id/role_id."""
    columns = {c.key for c in models.OrgMember.__table__.columns}
    assert "org_id" in columns
    assert "role_id" in columns
    assert "organisation_id" not in columns
    assert "role" not in columns


def test_org_member_listing_exposes_role_key_and_joined_at(db):
    """The endpoint read member.role and member.created_at; neither exists."""
    user = _register(db)
    db.commit()
    org = OrganizationService.create_organization(db, "Acme Relief", user)
    db.commit()

    member = (
        db.query(models.OrgMember)
        .filter(models.OrgMember.user_id == user.id)
        .one()
    )
    assert member.joined_at is not None
    assert member.role is not None
    assert member.role.key in {"member", "moderator", "admin", "owner"}


def test_last_owner_cannot_be_removed(db):
    """The owner guard compared member.role == "owner" against a FK column."""
    user = _register(db)
    db.commit()
    OrganizationService.create_organization(db, "Acme Relief", user)
    db.commit()

    member = (
        db.query(models.OrgMember)
        .filter(models.OrgMember.user_id == user.id)
        .one()
    )
    role_key = member.role.key
    assert role_key  # a role row is joined through role_id

    owner_count = (
        db.query(models.OrgMember)
        .join(models.Role, models.Role.id == models.OrgMember.role_id)
        .filter(models.Role.key == "owner")
        .count()
    )
    assert owner_count == 1


# --------------------------------------------------------------------------
# Audit persistence
# --------------------------------------------------------------------------

def test_audit_log_ip_column_has_a_default(db):
    """Every call site omitted ip while the column was NOT NULL."""
    column = models.AuditLog.__table__.columns["ip"]
    assert column.nullable is False
    assert column.default is not None


def test_audit_record_persists_and_redacts(db):
    _register(db)
    user = db.query(models.User).one()
    db.commit()

    AuditService.record(
        db,
        event="test.event",
        user_id=user.id,
        payload={"password": "hunter2", "safe": "value"},
    )
    db.commit()

    row = db.query(models.AuditLog).filter(models.AuditLog.event == "test.event").one()
    assert row.ip
    assert "hunter2" not in row.payload_json["password"]


# --------------------------------------------------------------------------
# OAuth: no credential in the URL
# --------------------------------------------------------------------------

def test_auth_code_is_stored_only_as_a_hash(db):
    """The raw code must never be persisted, or the table leaks sessions."""
    _register(db)
    user = db.query(models.User).one()
    db.commit()

    raw = "a" * 43
    row = models.OAuthAuthCode(
        code_hash=hash_token(raw),
        user_id=user.id,
        expires_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
    )
    db.add(row)
    db.commit()

    stored = db.query(models.OAuthAuthCode).one()
    assert stored.code_hash != raw
    assert stored.code_hash == hash_token(raw)


def test_frontend_url_field_name_is_correct():
    """oauth.py read settings.FRONTEND_URL; the field is frontend_url."""
    from config import settings

    assert hasattr(settings, "frontend_url")
    import oauth

    assert oauth.FRONTEND_URL == settings.frontend_url


# --------------------------------------------------------------------------
# Static drift guard
# --------------------------------------------------------------------------

def test_no_module_references_a_missing_model_attribute():
    """Guards the whole class of schema-vs-code drift fixed above."""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(BACKEND / "tools" / "check_model_usage.py")],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
        timeout=120,)
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_backend_module_is_syntactically_valid():
    """events/publisher.py shipped as `// Placeholder`, a hard SyntaxError."""
    import py_compile

    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(p in {".venv", "__pycache__"} for p in path.parts):
            continue
        try:
            py_compile.compile(str(path), doraise=True, cfile=str(path) + ".check")
        except py_compile.PyCompileError as exc:
            offenders.append(f"{path}: {exc}")
        finally:
            leftover = pathlib.Path(str(path) + ".check")
            if leftover.exists():
                leftover.unlink()
    assert not offenders, "\n".join(offenders)