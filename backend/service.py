
from datetime import timedelta, datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException, status
from . import models, schemas
from .security import hash_password, verify_password, create_access_token, create_refresh_token, hash_token
from .config import settings
import secrets

def register_user(db: Session, payload: schemas.UserCreate) -> models.User:
    # Create user
    user = models.User(
        display_name=payload.full_name,
        locale="en",
        status="active"
    )
    db.add(user)
    db.flush()  # Get user ID
    
    # Create email
    email = models.Email(
        user_id=user.id,
        email=payload.email,
        is_verified=False,
        is_primary=True
    )
    db.add(email)
    db.flush()
    
    # Set primary email
    user.primary_email_id = email.id
    
    # Create password credential
    password_cred = models.PasswordCredential(
        user_id=user.id,
        password_hash=hash_password(payload.password)
    )
    db.add(password_cred)
    
    try:
        db.commit()
        db.refresh(user)
        db.refresh(email)
        return user
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Email already registered")

def authenticate_user(db: Session, email: str, password: str) -> models.User:
    # Find user by email
    email_obj = db.query(models.Email).filter(models.Email.email == email).first()
    if not email_obj:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    
    user = email_obj.user
    if user.status != "active":
        raise HTTPException(status_code=400, detail="User is inactive")
    
    # Check password
    if not user.password_credential or not verify_password(password, user.password_credential.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    
    return user

def issue_tokens(user: models.User, db: Session) -> schemas.TokenPair:
    # Create session
    session = models.Session(
        user_id=user.id,
        device_id=secrets.token_hex(16),
        ip_first="127.0.0.1",
        ip_last="127.0.0.1",
        user_agent="Web Browser"
    )
    db.add(session)
    db.flush()
    
    # Create tokens
    access_token = create_access_token(user.id, session_id=session.id)
    refresh_token = create_refresh_token()
    
    # Store refresh token
    refresh_token_obj = models.RefreshToken(
        session_id=session.id,
        token_hash=hash_token(refresh_token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days)
    )
    db.add(refresh_token_obj)
    db.commit()
    
    return schemas.TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_min * 60
    )

def rotate_refresh(db: Session, refresh_token: str) -> schemas.TokenPair:
    # Find refresh token
    token_hash = hash_token(refresh_token)
    refresh_obj = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == token_hash,
        models.RefreshToken.revoked_at.is_(None),
        models.RefreshToken.expires_at > datetime.now(timezone.utc)
    ).first()
    
    if not refresh_obj:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    
    user = refresh_obj.session.user
    
    # Revoke old token
    refresh_obj.revoked_at = datetime.now(timezone.utc)
    
    # Create new tokens
    access_token = create_access_token(user.id, session_id=refresh_obj.session_id)
    new_refresh_token = create_refresh_token()
    
    # Store new refresh token
    new_refresh_obj = models.RefreshToken(
        session_id=refresh_obj.session_id,
        token_hash=hash_token(new_refresh_token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days)
    )
    db.add(new_refresh_obj)
    db.commit()
    
    return schemas.TokenPair(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=settings.access_token_ttl_min * 60
    )

def revoke_refresh(db: Session, refresh_token: str):
    token_hash = hash_token(refresh_token)
    refresh_obj = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == token_hash,
        models.RefreshToken.revoked_at.is_(None)
    ).first()
    
    if refresh_obj:
        refresh_obj.revoked_at = datetime.now(timezone.utc)
        db.commit()

def create_org(db: Session, name: str) -> models.Organisation:
    org = models.Organisation(
        name=name,
        slug=name.lower().replace(" ", "-"),
        owner_user_id="placeholder"  # Will be set by caller
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org

def add_member(db: Session, user_id: str, org_id: str, role: str = "member"):
    # This is a simplified version - you'd need to create proper roles first
    member = models.OrgMember(
        user_id=user_id,
        org_id=org_id,
        role_id="placeholder"  # Would need to create role system
    )
    db.add(member)
    db.commit()
    return member
