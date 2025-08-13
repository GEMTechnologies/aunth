from datetime import timedelta, datetime, timezone
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, or_, desc
from fastapi import HTTPException, status
from typing import Optional, List, Dict, Any
import secrets
import logging

import models, schemas
from security import (
    hash_password, verify_password, create_access_token, create_refresh_token, 
    hash_token, generate_verification_token, generate_device_id, SecurityManager
)
from config import settings
from context_service import ContextService

logger = logging.getLogger(__name__)

class AuthService:
    @staticmethod
    def register_user(db: Session, payload: schemas.UserCreate, ip_address: str = None) -> models.User:
        # Check if user already exists
        existing_user = db.query(models.User).join(models.Email).filter(
            models.Email.email == payload.email
        ).first()

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
        user = db.query(models.User).join(models.Email).filter(
            models.Email.email == email,
            models.Email.is_primary == True
        ).first()

        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not user.password_credential or not verify_password(password, user.password_credential.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if user.status != "active":
            raise HTTPException(status_code=401, detail="Account is not active")

        return user

class SessionService:
    @staticmethod
    def create_session(db: Session, user: models.User, ip_address: str = None, user_agent: str = None) -> models.Session:
        session = models.Session(
            user_id=user.id,
            device_id=generate_device_id(),
            ip_created=ip_address or "unknown",
            ip_last=ip_address or "unknown",
            user_agent=user_agent or "unknown"
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def issue_tokens(db: Session, user: models.User, session: models.Session) -> schemas.TokenPair:
        access_token = create_access_token(
            data={"sub": str(user.id), "sid": str(session.id)}
        )
        refresh_token = create_refresh_token(
            data={"sub": str(user.id), "sid": str(session.id)}
        )

        # Store refresh token hash
        session.refresh_token_hash = hash_token(refresh_token)
        db.commit()

        return schemas.TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=settings.access_token_ttl_min * 60
        )

    @staticmethod
    def rotate_refresh_token(db: Session, refresh_token: str) -> schemas.TokenPair:
        # Find session by refresh token hash
        token_hash = hash_token(refresh_token)
        session = db.query(models.Session).filter(
            models.Session.refresh_token_hash == token_hash,
            models.Session.revoked_at.is_(None)
        ).first()

        if not session:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        user = db.query(models.User).filter(models.User.id == session.user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")

        return SessionService.issue_tokens(db, user, session)

    @staticmethod
    def revoke_refresh_token(db: Session, refresh_token: str):
        token_hash = hash_token(refresh_token)
        session = db.query(models.Session).filter(
            models.Session.refresh_token_hash == token_hash
        ).first()

        if session:
            session.revoked_at = datetime.now(timezone.utc)
            db.commit()

    @staticmethod
    def revoke_session(db: Session, session_id: str):
        session = db.query(models.Session).filter(models.Session.id == session_id).first()
        if session:
            session.revoked_at = datetime.now(timezone.utc)
            db.commit()

class OrganizationService:
    @staticmethod
    def create_organization(db: Session, name: str, owner: models.User) -> models.Organisation:
        # Generate slug from name
        import re
        slug = re.sub(r'[^a-zA-Z0-9-]', '-', name.lower()).strip('-')

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

        # Add owner as admin member
        member = models.OrgMember(
            organisation_id=org.id,
            user_id=owner.id,
            role="admin"
        )
        db.add(member)

        db.commit()
        db.refresh(org)
        return org

    @staticmethod
    def add_member(db: Session, user_id: str, org_id: str, role: str = "member"):
        member = models.OrgMember(
            organisation_id=org_id,
            user_id=user_id,
            role=role
        )
        db.add(member)
        db.commit()
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
        user = db.query(models.User).join(models.Email).filter(
            models.Email.email == email.lower().strip(),
            models.Email.is_primary == True
        ).first()
        
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
        audit_log = models.AuditLog(
            user_id=user.id,
            event="password.reset_requested",
            payload_json={"email": email}
        )
        db.add(audit_log)
        
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
        audit_log = models.AuditLog(
            user_id=user.id,
            event="password.reset_completed",
            payload_json={"sessions_revoked": len(active_sessions)}
        )
        db.add(audit_log)
        
        db.commit()
        
        return {"message": "Password reset successfully"}

def add_member(db: Session, user_id: str, org_id: str, role: str = "member"):
    return OrganizationService.add_member(db, user_id, org_id, role)