from datetime import timedelta, datetime, timezone
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, or_, desc
from fastapi import HTTPException, status
from typing import Optional, List, Dict, Any
import secrets
import logging
import re

import models, schemas
from security import (
    hash_password, verify_password, create_access_token, create_refresh_token, 
    hash_token, generate_verification_token, generate_device_id, SecurityManager
)
from config import settings
from context_service import ContextService

logger = logging.getLogger(__name__)


def _as_utc(value):
    """Return a timezone-aware UTC datetime.

    Columns declared ``DateTime(timezone=True)`` come back aware from
    PostgreSQL but naive from SQLite and some drivers. Comparing a naive value
    with ``datetime.now(timezone.utc)`` raises TypeError, so every comparison
    against a stored timestamp goes through here.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

class AuthService:
    @staticmethod
    def register_user(db: Session, payload: schemas.UserCreate, ip_address: str = None) -> models.User:
        # Check if user already exists. The onclause is explicit because users and
        # emails reference each other and SQLAlchemy cannot infer the path.
        existing_user = (
            db.query(models.User)
            .join(models.Email, models.Email.user_id == models.User.id)
            .filter(models.Email.email == payload.email)
            .first()
        )

        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")

        # Create user
        user = models.User(
            display_name=payload.full_name or payload.email.split('@')[0],
            locale='en'
        )
        db.add(user)
        db.flush()

        # Create email
        email = models.Email(
            user_id=user.id,
            email=payload.email,
            is_primary=True,
            is_verified=False
        )
        db.add(email)
        db.flush()

        # Point the user at its primary email. Leaving this null made
        # user.primary_email silently empty for every registered account.
        user.primary_email_id = email.id

        # Create password credential
        if payload.password:
            password_cred = models.PasswordCredential(
                user_id=user.id,
                password_hash=hash_password(payload.password)
            )
            db.add(password_cred)

        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def authenticate_user(db: Session, email: str, password: str, ip_address: str = None, user_agent: str = None) -> models.User:
        # users and emails reference each other (emails.user_id and
        # users.primary_email_id), so SQLAlchemy cannot infer the join and
        # raises AmbiguousForeignKeysError. The onclause is now explicit.
        user = (
            db.query(models.User)
            .join(models.Email, models.Email.user_id == models.User.id)
            .filter(
                models.Email.email == email,
                models.Email.is_primary.is_(True),
            )
            .first()
        )

        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not user.password_credential or not verify_password(password, user.password_credential.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if user.status != "active":
            raise HTTPException(status_code=401, detail="Account is not active")

        return user

class TenantResolution:
    """The tenant and roles a user is acting as, resolved from the database."""

    def __init__(self, org_id: Optional[str], roles: List[str], org_ids: List[str]):
        self.org_id = org_id
        self.roles = roles
        self.org_ids = org_ids

    @property
    def is_resolved(self) -> bool:
        return bool(self.org_id)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TenantResolution org_id={self.org_id!r} roles={self.roles!r}>"


def resolve_tenant_for_user(db: Session, user_id: str) -> TenantResolution:
    """Resolve which organisation and roles a user belongs to.

    This is the single place tenant scope is derived. A user who belongs to no
    organisation yields ``org_id=None``, which callers must treat as DENY --
    never as a default tenant.
    """
    memberships = (
        db.query(models.OrgMember, models.Role)
        .join(models.Role, models.Role.id == models.OrgMember.role_id)
        .filter(models.OrgMember.user_id == user_id)
        .all()
    )

    org_ids = [membership[0].org_id for membership in memberships]
    roles = [membership[1].key for membership in memberships]

    # Prefer the organisation the user owns: it is unambiguous.
    owned = (
        db.query(models.Organisation)
        .filter(models.Organisation.owner_user_id == user_id)
        .first()
    )
    if owned:
        return TenantResolution(owned.id, roles or ["owner"], org_ids + [owned.id])

    if org_ids:
        return TenantResolution(org_ids[0], roles, org_ids)

    return TenantResolution(None, roles, [])


class AuditService:
    """Central entry point for writing audit records.

    Audit entries must persist. Direct construction of ``models.AuditLog`` is
    discouraged because it silently omits the NOT NULL ``ip`` column and
    offers no redaction, which is how credentials end up in an audit trail.
    """

    SENSITIVE_KEYS = frozenset({
        "password", "new_password", "old_password", "token", "access_token",
        "refresh_token", "secret", "client_secret", "authorization", "api_key",
        "otp", "code", "code_verifier", "session_token",
    })

    @classmethod
    def redact(cls, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Strip anything that must never be written to the audit trail."""
        if not payload:
            return {}
        redacted: Dict[str, Any] = {}
        for key, value in payload.items():
            if key.lower() in cls.SENSITIVE_KEYS:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = value
        return redacted

    @staticmethod
    def record(
        db: Session,
        event: str,
        user_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> models.AuditLog:
        """Persist an audit record in the caller's transaction."""
        entry = models.AuditLog(
            user_id=user_id,
            actor_user_id=actor_user_id or user_id,
            org_id=org_id,
            event=event,
            ip=ip_address or "unknown",
            user_agent=user_agent,
            payload_json=AuditService.redact(payload),
        )
        db.add(entry)
        return entry


class SessionService:
    @staticmethod
    def create_session(db: Session, user: models.User, ip_address: str = None, user_agent: str = None) -> models.Session:
        session = models.Session(
            user_id=user.id,
            device_id=generate_device_id(user_agent, ip_address),
            # The column is ip_first; this previously wrote ip_created, which
            # SQLAlchemy rejects as an unknown keyword argument.
            ip_first=ip_address or "unknown",
            ip_last=ip_address or "unknown",
            user_agent=user_agent or "unknown"
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def issue_tokens(db: Session, user: models.User, session: models.Session) -> schemas.TokenPair:
        """Mint a token pair scoped to the user's resolved tenant.

        Refresh tokens are opaque and stored hashed in ``refresh_tokens``, not
        on the session row. The previous code wrote to
        ``Session.refresh_token_hash``, a column that does not exist, so
        refresh rotation and reuse detection never ran.
        """
        tenant = resolve_tenant_for_user(db, user.id)

        access_token = create_access_token(
            subject=str(user.id),
            org_id=tenant.org_id,
            roles=tenant.roles,
            session_id=str(session.id),
            org_ids=tenant.org_ids,
        )
        refresh_token = create_refresh_token()

        db.add(models.RefreshToken(
            session_id=str(session.id),
            token_hash=hash_token(refresh_token),
            expires_at=datetime.now(timezone.utc)
            + timedelta(days=settings.refresh_token_ttl_days),
        ))
        db.commit()

        return schemas.TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=settings.access_token_ttl_min * 60
        )

    @staticmethod
    def rotate_refresh_token(db: Session, refresh_token: str) -> schemas.TokenPair:
        """Exchange a refresh token for a new pair, detecting replay.

        If a token that was already rotated is presented again, the whole
        session is revoked: that is the signal that a token was stolen.
        """
        token_hash = hash_token(refresh_token)
        stored = db.query(models.RefreshToken).filter(
            models.RefreshToken.token_hash == token_hash
        ).first()

        if not stored:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        if stored.reuse_flag:
            # Replay of an already-rotated token: treat as compromise.
            SessionService._revoke_session_cascade(db, stored.session_id)
            logger.warning("Refresh token reuse detected; session revoked")
            raise HTTPException(status_code=401, detail="Refresh token reuse detected")

        if stored.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Refresh token revoked")

        if _as_utc(stored.expires_at) < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="Refresh token expired")

        session = db.query(models.Session).filter(
            models.Session.id == stored.session_id,
            models.Session.revoked_at.is_(None),
        ).first()
        if not session:
            raise HTTPException(status_code=401, detail="Session is not active")

        user = db.query(models.User).filter(models.User.id == session.user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")

        # Retire the presented token and mark it as rotated, then issue a new
        # one bound to the same session.
        stored.revoked_at = datetime.now(timezone.utc)
        stored.rotated_at = datetime.now(timezone.utc)
        stored.reuse_flag = True
        db.commit()

        return SessionService.issue_tokens(db, user, session)

    @staticmethod
    def _revoke_session_cascade(db: Session, session_id: str):
        now = datetime.now(timezone.utc)
        for token in db.query(models.RefreshToken).filter(
            models.RefreshToken.session_id == session_id,
            models.RefreshToken.revoked_at.is_(None),
        ).all():
            token.revoked_at = now

        session = db.query(models.Session).filter(
            models.Session.id == session_id
        ).first()
        if session:
            session.revoked_at = now
        db.commit()

    @staticmethod
    def revoke_refresh_token(db: Session, refresh_token: str):
        stored = db.query(models.RefreshToken).filter(
            models.RefreshToken.token_hash == hash_token(refresh_token)
        ).first()
        if stored:
            stored.revoked_at = datetime.now(timezone.utc)
            db.commit()

    @staticmethod
    def revoke_session(db: Session, session_id: str):
        session = db.query(models.Session).filter(models.Session.id == session_id).first()
        if session:
            session.revoked_at = datetime.now(timezone.utc)
            db.commit()

class OrganizationService:
    @staticmethod
    def _ensure_role(db: Session, key: str, name: str) -> models.Role:
        """Fetch or create a Role row.

        OrgMember references roles by ``role_id``; there is no free-text role
        column. The previous code passed ``role="admin"``, which is not a
        column, so membership creation raised TypeError.
        """
        role = db.query(models.Role).filter(models.Role.key == key).first()
        if role:
            return role
        role = models.Role(key=key, name=name, is_system=True)
        db.add(role)
        db.flush()
        return role

    @staticmethod
    def create_organization(db: Session, name: str, owner: models.User) -> models.Organisation:
        # Generate slug from name. The pattern collapses a whole run of
        # separators into one hyphen; mapping character-by-character turned
        # "Water & Sanitation NGO" into "water---sanitation-ngo".
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:80]

        # Check if slug exists
        existing = db.query(models.Organisation).filter(models.Organisation.slug == slug).first()
        if existing:
            slug = f"{slug}-{secrets.token_hex(4)}"

        org = models.Organisation(
            name=name,
            slug=slug,
            owner_user_id=owner.id,
            created_by=owner.id
        )
        db.add(org)
        db.flush()

        # Add owner as admin member.
        # OrgMember keys are (org_id, user_id) and carries role_id, not a
        # free-text role.
        owner_role = OrganizationService._ensure_role(db, "owner", "Owner")
        member = models.OrgMember(
            org_id=org.id,
            user_id=owner.id,
            role_id=owner_role.id
        )
        db.add(member)

        db.commit()
        db.refresh(org)
        return org

    @staticmethod
    def add_member(db: Session, user_id: str, org_id: str, role: str = "member"):
        role_row = OrganizationService._ensure_role(db, role, role.replace("_", " ").title())
        member = models.OrgMember(
            org_id=org_id,
            user_id=user_id,
            role_id=role_row.id
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        return member

# Legacy compatibility functions
def register_user(db: Session, payload: schemas.UserCreate) -> models.User:
    return AuthService.register_user(db, payload)

def authenticate_user(db: Session, email: str, password: str) -> models.User:
    return AuthService.authenticate_user(db, email, password)

def issue_tokens(user: models.User, db: Session) -> schemas.TokenPair:
    session = SessionService.create_session(db, user)
    return SessionService.issue_tokens(db, user, session)

def rotate_refresh(db: Session, refresh_token: str) -> schemas.TokenPair:
    return SessionService.rotate_refresh_token(db, refresh_token)

def revoke_refresh(db: Session, refresh_token: str):
    SessionService.revoke_refresh_token(db, refresh_token)

def create_org(db: Session, name: str) -> models.Organisation:
    raise HTTPException(status_code=501, detail="Use OrganizationService.create_organization")

class PasswordResetService:
    @staticmethod
    def request_password_reset(db: Session, email: str) -> Dict[str, str]:
        """Request password reset for email"""
        user = (
            db.query(models.User)
            .join(models.Email, models.Email.user_id == models.User.id)
            .filter(
                models.Email.email == email.lower().strip(),
                models.Email.is_primary.is_(True),
            )
            .first()
        )
        
        if not user:
            # Don't reveal if email exists - always return success message
            return {"message": "If the email exists, a reset link has been sent"}
        
        # Generate reset token
        reset_token = generate_verification_token()
        token_hash = hash_token(reset_token)
        
        # Create password reset record
        reset_record = models.PasswordReset(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        db.add(reset_record)
        
        # Create audit log
        AuditService.record(
            db,
            event="password.reset_requested",
            user_id=user.id,
            payload={"email": email},
        )

        db.commit()
        
        # TODO: Send email with reset_token
        logger.info(f"Password reset requested for user: {user.id}")
        
        return {"message": "If the email exists, a reset link has been sent"}
    
    @staticmethod
    def reset_password(db: Session, token: str, new_password: str) -> Dict[str, str]:
        """Reset password using token"""
        token_hash = hash_token(token)
        
        reset_record = db.query(models.PasswordReset).filter(
            models.PasswordReset.token_hash == token_hash,
            models.PasswordReset.used_at.is_(None),
            models.PasswordReset.expires_at > datetime.now(timezone.utc)
        ).first()
        
        if not reset_record:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")
        
        user = db.query(models.User).filter(models.User.id == reset_record.user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Update password
        if user.password_credential:
            user.password_credential.password_hash = hash_password(new_password)
            user.password_credential.updated_at = datetime.now(timezone.utc)
        else:
            password_cred = models.PasswordCredential(
                user_id=user.id,
                password_hash=hash_password(new_password)
            )
            db.add(password_cred)
        
        # Mark reset token as used
        reset_record.used_at = datetime.now(timezone.utc)
        
        # Revoke all sessions for security
        active_sessions = db.query(models.Session).filter(
            models.Session.user_id == user.id,
            models.Session.revoked_at.is_(None)
        ).all()
        
        for session in active_sessions:
            session.revoked_at = datetime.now(timezone.utc)
        
        # Create audit log
        AuditService.record(
            db,
            event="password.reset_completed",
            user_id=user.id,
            payload={"sessions_revoked": len(active_sessions)},
        )

        db.commit()
        
        return {"message": "Password reset successfully"}

def add_member(db: Session, user_id: str, org_id: str, role: str = "member"):
    return OrganizationService.add_member(db, user_id, org_id, role)