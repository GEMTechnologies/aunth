
from fastapi import APIRouter, Depends, HTTPException, status, Header, Request
from fastapi.security import OAuth2PasswordBearer, HTTPBearer
from sqlalchemy.orm import Session, joinedload
from typing import Optional, List
from jose import JWTError
from datetime import datetime, timezone
import logging

from .database import get_db
from . import schemas, models
from .security import decode_access_token
from .service import AuthService, SessionService, OrganizationService
from .config import settings

logger = logging.getLogger(__name__)

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")
bearer_scheme = HTTPBearer()

def get_client_ip(request: Request) -> str:
    """Extract client IP address from request"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host

def get_user_agent(request: Request) -> str:
    """Extract user agent from request"""
    return request.headers.get("User-Agent", "Unknown")

def get_current_user(
    request: Request,
    db: Session = Depends(get_db), 
    token: str = Depends(oauth2_scheme)
) -> models.User:
    """Get current authenticated user from JWT token"""
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        
        if user.status != "active":
            raise HTTPException(status_code=401, detail="User account is not active")
        
        # Update last seen for session if session_id is in token
        session_id = payload.get("sid")
        if session_id:
            session = db.query(models.Session).filter(
                models.Session.id == session_id,
                models.Session.user_id == user.id,
                models.Session.revoked_at.is_(None)
            ).first()
            if session:
                session.last_seen_at = datetime.now(timezone.utc)
                session.ip_last = get_client_ip(request)
                db.commit()
        
        return user
        
    except JWTError as e:
        logger.warning(f"JWT error: {str(e)}")
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        logger.error(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=401, detail="Authentication failed")

def get_current_active_user(current_user: models.User = Depends(get_current_user)) -> models.User:
    """Ensure current user is active"""
    if current_user.status != "active":
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user

# Authentication endpoints
@router.post("/auth/register", response_model=schemas.UserResponse, tags=["Authentication"])
def register(
    request: Request,
    payload: schemas.RegisterRequest, 
    db: Session = Depends(get_db)
):
    """Register new user account"""
    try:
        user_create = schemas.UserCreate(
            email=payload.email,
            password=payload.password,
            full_name=payload.full_name
        )
        
        user = AuthService.register_user(
            db, 
            user_create, 
            ip_address=get_client_ip(request)
        )
        
        return schemas.UserResponse(
            id=user.id,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            locale=user.locale,
            created_at=user.created_at,
            status=user.status,
            primary_email=schemas.EmailResponse(
                id=user.primary_email.id,
                email=user.primary_email.email,
                is_verified=user.primary_email.is_verified,
                is_primary=user.primary_email.is_primary,
                created_at=user.primary_email.created_at
            ) if user.primary_email else None
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Registration error: {str(e)}")
        raise HTTPException(status_code=500, detail="Registration failed")

@router.post("/auth/login", response_model=schemas.TokenResponse, tags=["Authentication"])
def login(
    request: Request,
    payload: schemas.LoginRequest, 
    db: Session = Depends(get_db)
):
    """Authenticate user and return tokens"""
    try:
        user = AuthService.authenticate_user(
            db, 
            payload.email, 
            payload.password,
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request)
        )
        
        session = SessionService.create_session(
            db, 
            user,
            ip_address=get_client_ip(request),
            user_agent=get_user_agent(request)
        )
        
        tokens = SessionService.issue_tokens(db, user, session)
        
        return schemas.TokenResponse(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            token_type=tokens.token_type,
            expires_in=tokens.expires_in,
            user=schemas.UserResponse(
                id=user.id,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                locale=user.locale,
                created_at=user.created_at,
                status=user.status,
                primary_email=schemas.EmailResponse(
                    id=user.primary_email.id,
                    email=user.primary_email.email,
                    is_verified=user.primary_email.is_verified,
                    is_primary=user.primary_email.is_primary,
                    created_at=user.primary_email.created_at
                ) if user.primary_email else None
            )
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        raise HTTPException(status_code=500, detail="Login failed")

@router.post("/auth/refresh", response_model=schemas.TokenPair, tags=["Authentication"])
def refresh_token(
    authorization: Optional[str] = Header(default=None), 
    db: Session = Depends(get_db)
):
    """Refresh access token using refresh token"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid refresh token")
    
    refresh_token = authorization.split(" ", 1)[1]
    
    try:
        tokens = SessionService.rotate_refresh_token(db, refresh_token)
        return tokens
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {str(e)}")
        raise HTTPException(status_code=401, detail="Token refresh failed")

@router.post("/auth/logout", status_code=204, tags=["Authentication"])
def logout(
    authorization: Optional[str] = Header(default=None), 
    db: Session = Depends(get_db)
):
    """Logout user by revoking refresh token"""
    if authorization and authorization.lower().startswith("bearer "):
        refresh_token = authorization.split(" ", 1)[1]
        try:
            SessionService.revoke_refresh_token(db, refresh_token)
        except Exception as e:
            logger.warning(f"Logout error: {str(e)}")
            # Don't fail logout even if token revocation fails
    
    return

@router.post("/auth/logout-all", status_code=204, tags=["Authentication"])
def logout_all_sessions(
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Logout user from all sessions"""
    try:
        # Revoke all active sessions for user
        active_sessions = db.query(models.Session).filter(
            models.Session.user_id == current_user.id,
            models.Session.revoked_at.is_(None)
        ).all()
        
        for session in active_sessions:
            SessionService.revoke_session(db, session.id)
        
        logger.info(f"All sessions revoked for user: {current_user.id}")
        
    except Exception as e:
        logger.error(f"Logout all error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to logout from all sessions")

# User management endpoints
@router.get("/users/me", response_model=schemas.MeResponse, tags=["Users"])
def get_current_user_profile(
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get current user profile with detailed information"""
    
    # Load user with relationships
    user = db.query(models.User).options(
        joinedload(models.User.emails),
        joinedload(models.User.sessions)
    ).filter(models.User.id == current_user.id).first()
    
    return schemas.MeResponse(
        id=user.id,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        locale=user.locale,
        created_at=user.created_at,
        status=user.status,
        emails=[
            schemas.EmailResponse(
                id=email.id,
                email=email.email,
                is_verified=email.is_verified,
                is_primary=email.is_primary,
                created_at=email.created_at
            ) for email in user.emails
        ],
        sessions=[
            schemas.SessionResponse(
                id=session.id,
                device_id=session.device_id,
                user_agent=session.user_agent,
                ip_last=session.ip_last,
                created_at=session.created_at,
                last_seen_at=session.last_seen_at
            ) for session in user.sessions if not session.revoked_at
        ]
    )

@router.patch("/users/me", response_model=schemas.UserResponse, tags=["Users"])
def update_user_profile(
    payload: schemas.UpdateProfileRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Update current user profile"""
    try:
        if payload.display_name is not None:
            current_user.display_name = payload.display_name
        
        if payload.locale is not None:
            current_user.locale = payload.locale
        
        # Create audit log
        audit_log = models.AuditLog(
            user_id=current_user.id,
            actor_user_id=current_user.id,
            event="user.profile_updated",
            payload_json={
                "display_name": payload.display_name,
                "locale": payload.locale
            }
        )
        db.add(audit_log)
        
        db.commit()
        db.refresh(current_user)
        
        return schemas.UserResponse(
            id=current_user.id,
            display_name=current_user.display_name,
            avatar_url=current_user.avatar_url,
            locale=current_user.locale,
            created_at=current_user.created_at,
            status=current_user.status,
            primary_email=schemas.EmailResponse(
                id=current_user.primary_email.id,
                email=current_user.primary_email.email,
                is_verified=current_user.primary_email.is_verified,
                is_primary=current_user.primary_email.is_primary,
                created_at=current_user.primary_email.created_at
            ) if current_user.primary_email else None
        )
        
    except Exception as e:
        logger.error(f"Profile update error: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update profile")

# Organization endpoints
@router.post("/organizations", response_model=schemas.OrganisationResponse, tags=["Organizations"])
def create_organization(
    payload: schemas.OrgCreate, 
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Create new organization"""
    try:
        org = OrganizationService.create_organization(db, payload.name, current_user)
        
        return schemas.OrganisationResponse(
            id=org.id,
            name=org.name,
            slug=org.slug,
            created_at=org.created_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Organization creation error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create organization")

@router.get("/organizations", response_model=List[schemas.OrganisationResponse], tags=["Organizations"])
def list_user_organizations(
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """List organizations where user is a member"""
    try:
        # Get user's organization memberships
        memberships = db.query(models.OrgMember).options(
            joinedload(models.OrgMember.organisation)
        ).filter(models.OrgMember.user_id == current_user.id).all()
        
        return [
            schemas.OrganisationResponse(
                id=membership.organisation.id,
                name=membership.organisation.name,
                slug=membership.organisation.slug,
                created_at=membership.organisation.created_at
            ) for membership in memberships
        ]
        
    except Exception as e:
        logger.error(f"Organization listing error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list organizations")

# Health check endpoint
@router.get("/health", tags=["System"])
def health_check(db: Session = Depends(get_db)):
    """Health check endpoint"""
    try:
        # Test database connection
        db.execute("SELECT 1")
        return {
            "status": "healthy",
            "service": "granada-auth",
            "version": settings.api_version
        }
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        raise HTTPException(status_code=503, detail="Service unhealthy")

# Legacy compatibility endpoints
@router.post("/auth/register", response_model=schemas.UserRead, tags=["Authentication", "Legacy"])
def register_legacy(payload: schemas.UserCreate, db: Session = Depends(get_db)):
    """Legacy registration endpoint for backward compatibility"""
    user = AuthService.register_user(db, payload)
    return schemas.UserRead(
        id=user.id, 
        email=user.primary_email.email if user.primary_email else "", 
        full_name=user.display_name, 
        is_verified=user.primary_email.is_verified if user.primary_email else False
    )

@router.get("/users/me", response_model=schemas.UserRead, tags=["Users", "Legacy"])
def get_me_legacy(current: models.User = Depends(get_current_user)):
    """Legacy user profile endpoint"""
    return schemas.UserRead(
        id=current.id, 
        email=current.primary_email.email if current.primary_email else "", 
        full_name=current.display_name, 
        is_verified=current.primary_email.is_verified if current.primary_email else False
    )

@router.post("/orgs", response_model=schemas.OrgRead, tags=["Organizations", "Legacy"])
def create_org_legacy(
    payload: schemas.OrgCreate, 
    db: Session = Depends(get_db), 
    current: models.User = Depends(get_current_user)
):
    """Legacy organization creation endpoint"""
    org = OrganizationService.create_organization(db, payload.name, current)
    return schemas.OrgRead(id=org.id, name=org.name)
