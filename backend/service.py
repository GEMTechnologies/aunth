
from datetime import timedelta, datetime, timezone
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, or_, desc
from fastapi import HTTPException, status
from typing import Optional, List, Dict, Any
import secrets
import logging

from . import models, schemas
from .security import (
    hash_password, verify_password, create_access_token, create_refresh_token, 
    hash_token, generate_verification_token, generate_device_id, SecurityManager
)
from .config import settings
from .context_service import ContextService

logger = logging.getLogger(__name__)

class AuthService:
    """Authentication service with comprehensive user management"""
    
    @staticmethod
    def register_user(db: Session, payload: schemas.UserCreate, ip_address: str = "127.0.0.1", intent: Optional[str] = None) -> models.User:
        """Register new user with email and password"""
        try:
            # Check password strength
            password_analysis = SecurityManager.check_password_strength(payload.password)
            if password_analysis["score"] < 2:
                raise HTTPException(
                    status_code=400, 
                    detail="Password is too weak. Must contain at least 8 characters with mix of letters, numbers, and symbols."
                )
            
            # Create user
            user = models.User(
                display_name=payload.full_name or payload.email.split('@')[0],
                locale="en",
                status="active",
                registration_intent=intent
            )
            db.add(user)
            db.flush()  # Get user ID
            
            # Create primary email
            email = models.Email(
                user_id=user.id,
                email=payload.email.lower().strip(),
                is_verified=False,
                is_primary=True
            )
            db.add(email)
            db.flush()
            
            # Set primary email reference
            user.primary_email_id = email.id
            
            # Create password credential
            password_cred = models.PasswordCredential(
                user_id=user.id,
                password_hash=hash_password(payload.password),
                password_version=1
            )
            db.add(password_cred)
            
            # Create context based on intent
            if intent == "student":
                ContextService.create_student_context(db, user.id)
            # For NGO/business intents, context will be created when org is created
            
            # Create audit log
            audit_log = models.AuditLog(
                user_id=user.id,
                event="user.registered",
                ip=ip_address,
                payload_json={
                    "email": payload.email,
                    "registration_method": "email_password",
                    "intent": intent
                }
            )
            db.add(audit_log)
            
            db.commit()
            db.refresh(user)
            db.refresh(email)
            
            logger.info(f"User registered successfully: {user.id}")
            return user
            
        except IntegrityError as e:
            db.rollback()
            if "email" in str(e):
                raise HTTPException(status_code=400, detail="Email already registered")
            raise HTTPException(status_code=400, detail="Registration failed")
        except Exception as e:
            db.rollback()
            logger.error(f"Registration failed: {str(e)}")
            raise
    
    @staticmethod
    def authenticate_user(
        db: Session, 
        email: str, 
        password: str, 
        ip_address: str = "127.0.0.1",
        user_agent: str = "Unknown"
    ) -> models.User:
        """Authenticate user with email and password"""
        
        # Find user by email
        email_obj = db.query(models.Email).filter(
            models.Email.email == email.lower().strip()
        ).first()
        
        if not email_obj:
            # Log failed attempt
            audit_log = models.AuditLog(
                event="auth.failed",
                ip=ip_address,
                user_agent=user_agent,
                payload_json={"email": email, "reason": "email_not_found"}
            )
            db.add(audit_log)
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        user = email_obj.user
        
        # Check user status
        if user.status != "active":
            audit_log = models.AuditLog(
                user_id=user.id,
                event="auth.failed",
                ip=ip_address,
                user_agent=user_agent,
                payload_json={"reason": "user_inactive", "status": user.status}
            )
            db.add(audit_log)
            db.commit()
            raise HTTPException(status_code=400, detail="Account is not active")
        
        # Check password
        if not user.password_credential or not verify_password(password, user.password_credential.password_hash):
            audit_log = models.AuditLog(
                user_id=user.id,
                event="auth.failed",
                ip=ip_address,
                user_agent=user_agent,
                payload_json={"reason": "invalid_password"}
            )
            db.add(audit_log)
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        # Log successful authentication
        audit_log = models.AuditLog(
            user_id=user.id,
            event="auth.success",
            ip=ip_address,
            user_agent=user_agent,
            payload_json={"login_method": "password"}
        )
        db.add(audit_log)
        db.commit()
        
        logger.info(f"User authenticated successfully: {user.id}")
        return user

class SessionService:
    """Session and token management service"""
    
    @staticmethod
    def create_session(
        db: Session,
        user: models.User,
        ip_address: str = "127.0.0.1",
        user_agent: str = "Unknown"
    ) -> models.Session:
        """Create new user session"""
        
        # Generate device ID
        device_id = generate_device_id(user_agent, ip_address)
        
        # Check for existing session on same device
        existing_session = db.query(models.Session).filter(
            models.Session.user_id == user.id,
            models.Session.device_id == device_id,
            models.Session.revoked_at.is_(None)
        ).first()
        
        if existing_session:
            # Update existing session
            existing_session.ip_last = ip_address
            existing_session.last_seen_at = datetime.now(timezone.utc)
            existing_session.user_agent = user_agent
            db.commit()
            return existing_session
        
        # Create new session
        session = models.Session(
            user_id=user.id,
            device_id=device_id,
            user_agent=user_agent,
            ip_first=ip_address,
            ip_last=ip_address
        )
        db.add(session)
        db.flush()
        
        # Clean up old sessions if user has too many
        user_sessions = db.query(models.Session).filter(
            models.Session.user_id == user.id,
            models.Session.revoked_at.is_(None)
        ).order_by(desc(models.Session.last_seen_at)).all()
        
        if len(user_sessions) > settings.max_sessions_per_user:
            # Revoke oldest sessions
            sessions_to_revoke = user_sessions[settings.max_sessions_per_user:]
            for old_session in sessions_to_revoke:
                old_session.revoked_at = datetime.now(timezone.utc)
        
        db.commit()
        return session
    
    @staticmethod
    def issue_tokens(
        db: Session, 
        user: models.User, 
        session: models.Session,
        org_id: Optional[str] = None
    ) -> schemas.TokenPair:
        """Issue access and refresh tokens for session"""
        
        # Get user permissions (implement based on your RBAC needs)
        roles = []  # TODO: Implement role fetching
        permissions = []  # TODO: Implement permission fetching
        
        # Create access token
        access_token = create_access_token(
            subject=user.id,
            org_id=org_id,
            roles=roles,
            permissions=permissions,
            session_id=session.id
        )
        
        # Create refresh token
        refresh_token = create_refresh_token()
        
        # Store refresh token
        refresh_token_obj = models.RefreshToken(
            session_id=session.id,
            token_hash=hash_token(refresh_token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days)
        )
        db.add(refresh_token_obj)
        
        # Revoke old refresh tokens for this session if token rotation is enabled
        if settings.token_rotation:
            old_tokens = db.query(models.RefreshToken).filter(
                models.RefreshToken.session_id == session.id,
                models.RefreshToken.revoked_at.is_(None)
            ).all()
            
            for old_token in old_tokens[:-1]:  # Keep the newest one
                old_token.revoked_at = datetime.now(timezone.utc)
        
        db.commit()
        
        return schemas.TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.access_token_ttl_min * 60
        )
    
    @staticmethod
    def rotate_refresh_token(db: Session, refresh_token: str) -> schemas.TokenPair:
        """Rotate refresh token and issue new token pair"""
        
        # Find refresh token
        token_hash = hash_token(refresh_token)
        refresh_obj = db.query(models.RefreshToken).options(
            joinedload(models.RefreshToken.session).joinedload(models.Session.user)
        ).filter(
            models.RefreshToken.token_hash == token_hash,
            models.RefreshToken.revoked_at.is_(None),
            models.RefreshToken.expires_at > datetime.now(timezone.utc)
        ).first()
        
        if not refresh_obj:
            raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
        
        # Check for token reuse
        if refresh_obj.reuse_flag:
            # Token reuse detected - revoke all tokens for this session
            db.query(models.RefreshToken).filter(
                models.RefreshToken.session_id == refresh_obj.session_id
            ).update({"revoked_at": datetime.now(timezone.utc)})
            
            refresh_obj.session.revoked_at = datetime.now(timezone.utc)
            db.commit()
            
            logger.warning(f"Token reuse detected for session: {refresh_obj.session_id}")
            raise HTTPException(status_code=401, detail="Token reuse detected")
        
        user = refresh_obj.session.user
        session = refresh_obj.session
        
        # Mark current token as used
        refresh_obj.reuse_flag = True
        refresh_obj.revoked_at = datetime.now(timezone.utc)
        
        # Update session last seen
        session.last_seen_at = datetime.now(timezone.utc)
        
        # Issue new tokens
        new_tokens = SessionService.issue_tokens(db, user, session)
        
        return new_tokens
    
    @staticmethod
    def revoke_refresh_token(db: Session, refresh_token: str):
        """Revoke a specific refresh token"""
        token_hash = hash_token(refresh_token)
        refresh_obj = db.query(models.RefreshToken).filter(
            models.RefreshToken.token_hash == token_hash,
            models.RefreshToken.revoked_at.is_(None)
        ).first()
        
        if refresh_obj:
            refresh_obj.revoked_at = datetime.now(timezone.utc)
            db.commit()
    
    @staticmethod
    def revoke_session(db: Session, session_id: str):
        """Revoke entire session and all associated tokens"""
        session = db.query(models.Session).filter(
            models.Session.id == session_id,
            models.Session.revoked_at.is_(None)
        ).first()
        
        if session:
            # Revoke session
            session.revoked_at = datetime.now(timezone.utc)
            
            # Revoke all refresh tokens for this session
            db.query(models.RefreshToken).filter(
                models.RefreshToken.session_id == session_id,
                models.RefreshToken.revoked_at.is_(None)
            ).update({"revoked_at": datetime.now(timezone.utc)})
            
            db.commit()

class OrganizationService:
    """Organization and membership management"""
    
    @staticmethod
    def create_organization(db: Session, name: str, owner: models.User) -> models.Organisation:
        """Create new organization with owner"""
        # Generate unique slug
        base_slug = name.lower().replace(" ", "-").replace("_", "-")
        slug = base_slug
        counter = 1
        
        while db.query(models.Organisation).filter(models.Organisation.slug == slug).first():
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        # Create organization
        org = models.Organisation(
            name=name,
            slug=slug,
            owner_user_id=owner.id
        )
        db.add(org)
        db.flush()
        
        # Create owner role (you'll need to implement role system)
        # For now, we'll skip the role creation
        
        # Add owner as member
        OrganizationService.add_member(db, owner.id, org.id, "owner")
        
        # Create audit log
        audit_log = models.AuditLog(
            user_id=owner.id,
            actor_user_id=owner.id,
            event="org.created",
            payload_json={
                "org_id": org.id,
                "org_name": name,
                "org_slug": slug
            }
        )
        db.add(audit_log)
        
        db.commit()
        db.refresh(org)
        
        logger.info(f"Organization created: {org.id} by user: {owner.id}")
        return org
    
    @staticmethod
    def add_member(db: Session, user_id: str, org_id: str, role: str = "member") -> models.OrgMember:
        """Add user to organization with specified role"""
        
        # Check if user is already a member
        existing = db.query(models.OrgMember).filter(
            models.OrgMember.user_id == user_id,
            models.OrgMember.org_id == org_id
        ).first()
        
        if existing:
            raise HTTPException(status_code=400, detail="User is already a member")
        
        # Create membership (simplified - you'll need proper role system)
        member = models.OrgMember(
            user_id=user_id,
            org_id=org_id,
            role_id="placeholder"  # TODO: Implement proper role system
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        
        return member

# Legacy functions for backward compatibility
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
    # This needs an owner - you'll need to pass it properly
    # For now, return a placeholder
    raise HTTPException(status_code=501, detail="Use OrganizationService.create_organization")

def add_member(db: Session, user_id: str, org_id: str, role: str = "member"):
    return OrganizationService.add_member(db, user_id, org_id, role)
