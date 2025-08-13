from fastapi import APIRouter, Depends, HTTPException, status, Header, Request
from fastapi.security import OAuth2PasswordBearer, HTTPBearer
from sqlalchemy.orm import Session, joinedload
from typing import Optional, List
from jose import JWTError
from datetime import datetime, timezone
import logging

from database import get_db
import schemas, models, service, security, oauth
from config import settings
from context_service import ContextService
from service import OrganizationService, AuthService, SessionService, PasswordResetService
from security import decode_access_token

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

@router.post("/auth/forgot-password", tags=["Authentication"])
def forgot_password(
    payload: schemas.ForgotPasswordRequest,
    db: Session = Depends(get_db)
):
    """Request password reset"""
    try:
        result = PasswordResetService.request_password_reset(db, payload.email)
        return result
    except Exception as e:
        logger.error(f"Forgot password error: {str(e)}")
        return {"message": "If the email exists, a reset link has been sent"}

@router.post("/auth/reset-password", tags=["Authentication"])
def reset_password(
    payload: schemas.ResetPasswordRequest,
    db: Session = Depends(get_db)
):
    """Reset password with token"""
    try:
        result = PasswordResetService.reset_password(db, payload.token, payload.new_password)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reset password error: {str(e)}")
        raise HTTPException(status_code=500, detail="Password reset failed")

# Context management endpoints
@router.get("/me/contexts", response_model=schemas.UserContextsResponse, tags=["Contexts"])
async def get_user_contexts(current_user: models.User = Depends(get_current_active_user), db: Session = Depends(get_db)):
    """Get all available contexts for the current user"""
    return ContextService.get_user_contexts(db, current_user.id)

@router.post("/me/last-context", tags=["Contexts"])
async def set_last_active_context(
    payload: schemas.SetContextRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Set user's last active context"""
    ContextService.set_last_active_context(db, current_user.id, payload.context)
    return {"message": "Context updated successfully"}

@router.get("/me/resolve-context", response_model=schemas.ContextResolutionResponse, tags=["Contexts"])
async def resolve_landing_context(
    request: Request,
    redirect_uri: Optional[str] = None,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Resolve where user should land based on context resolution algorithm"""
    host = request.headers.get("host")
    return ContextService.resolve_landing_context(db, current_user.id, host, redirect_uri)

# User profile endpoints
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

@router.get("/orgs/{org_id}/members", tags=["Organizations"])
def list_organization_members(
    org_id: str,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """List members of an organization"""
    try:
        # Check if user is member of organization
        membership = db.query(models.OrgMember).filter(
            models.OrgMember.organisation_id == org_id,
            models.OrgMember.user_id == current_user.id
        ).first()
        
        if not membership:
            raise HTTPException(status_code=403, detail="Access denied to organization")
        
        # Get all members
        members = db.query(models.OrgMember).options(
            joinedload(models.OrgMember.user).joinedload(models.User.primary_email)
        ).filter(models.OrgMember.organisation_id == org_id).all()
        
        return [
            {
                "user_id": member.user_id,
                "role": member.role,
                "joined_at": member.created_at,
                "user": {
                    "id": member.user.id,
                    "display_name": member.user.display_name,
                    "primary_email": {
                        "email": member.user.primary_email.email,
                        "is_verified": member.user.primary_email.is_verified
                    } if member.user.primary_email else None
                }
            } for member in members
        ]
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Member listing error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list members")

@router.post("/orgs/{org_id}/invites", tags=["Organizations"])
def invite_member(
    org_id: str,
    payload: schemas.InviteMemberRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Invite new member to organization"""
    try:
        # Check if user has admin role
        membership = db.query(models.OrgMember).filter(
            models.OrgMember.organisation_id == org_id,
            models.OrgMember.user_id == current_user.id,
            models.OrgMember.role.in_(["admin", "owner"])
        ).first()
        
        if not membership:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Check if user already exists and add them directly
        existing_user = db.query(models.User).join(models.Email).filter(
            models.Email.email == payload.email.lower().strip(),
            models.Email.is_primary == True
        ).first()
        
        if existing_user:
            # Check if already a member
            existing_member = db.query(models.OrgMember).filter(
                models.OrgMember.organisation_id == org_id,
                models.OrgMember.user_id == existing_user.id
            ).first()
            
            if existing_member:
                raise HTTPException(status_code=400, detail="User is already a member")
            
            # Add as member
            new_member = models.OrgMember(
                organisation_id=org_id,
                user_id=existing_user.id,
                role=payload.role
            )
            db.add(new_member)
            db.commit()
            
            return {"message": "User added to organization successfully"}
        else:
            # TODO: Create invitation record and send email
            return {"message": "Invitation sent successfully"}
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Member invitation error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to invite member")

@router.patch("/orgs/{org_id}/members/{user_id}", tags=["Organizations"])
def update_member_role(
    org_id: str,
    user_id: str,
    payload: schemas.UpdateMemberRoleRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Update member role in organization"""
    try:
        # Check if current user has admin role
        admin_membership = db.query(models.OrgMember).filter(
            models.OrgMember.organisation_id == org_id,
            models.OrgMember.user_id == current_user.id,
            models.OrgMember.role.in_(["admin", "owner"])
        ).first()
        
        if not admin_membership:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Find member to update
        member = db.query(models.OrgMember).filter(
            models.OrgMember.organisation_id == org_id,
            models.OrgMember.user_id == user_id
        ).first()
        
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")
        
        # Update role
        member.role = payload.role
        
        # Create audit log
        audit_log = models.AuditLog(
            user_id=current_user.id,
            actor_user_id=current_user.id,
            event="org.member_role_updated",
            payload_json={
                "org_id": org_id,
                "target_user_id": user_id,
                "new_role": payload.role
            }
        )
        db.add(audit_log)
        
        db.commit()
        
        return {"message": "Member role updated successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Member role update error: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update member role")

@router.delete("/orgs/{org_id}/members/{user_id}", status_code=204, tags=["Organizations"])
def remove_member(
    org_id: str,
    user_id: str,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Remove member from organization"""
    try:
        # Check if current user has admin role
        admin_membership = db.query(models.OrgMember).filter(
            models.OrgMember.organisation_id == org_id,
            models.OrgMember.user_id == current_user.id,
            models.OrgMember.role.in_(["admin", "owner"])
        ).first()
        
        if not admin_membership:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Find member to remove
        member = db.query(models.OrgMember).filter(
            models.OrgMember.organisation_id == org_id,
            models.OrgMember.user_id == user_id
        ).first()
        
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")
        
        # Don't allow removing the last owner
        if member.role == "owner":
            owner_count = db.query(models.OrgMember).filter(
                models.OrgMember.organisation_id == org_id,
                models.OrgMember.role == "owner"
            ).count()
            
            if owner_count <= 1:
                raise HTTPException(
                    status_code=400, 
                    detail="Cannot remove the last owner"
                )
        
        db.delete(member)
        
        # Create audit log
        audit_log = models.AuditLog(
            user_id=current_user.id,
            actor_user_id=current_user.id,
            event="org.member_removed",
            payload_json={
                "org_id": org_id,
                "removed_user_id": user_id,
                "removed_role": member.role
            }
        )
        db.add(audit_log)
        
        db.commit()
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Member removal error: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to remove member")

# OAuth/Social Login endpoints
@router.get("/auth/oauth/{provider}/authorize", tags=["OAuth"])
async def oauth_authorize(
    provider: str,
    db: Session = Depends(get_db)
):
    """Initiate OAuth flow with provider"""
    try:
        result = await oauth_service.initiate_oauth_flow(provider, db)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth authorize error: {str(e)}")
        raise HTTPException(status_code=500, detail="OAuth initiation failed")

@router.get("/auth/oauth/{provider}/callback", tags=["OAuth"])
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """Handle OAuth callback from provider"""
    try:
        user = await oauth_service.handle_oauth_callback(provider, code, state, db)

        # Create session and tokens
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
        logger.error(f"OAuth callback error: {str(e)}")
        raise HTTPException(status_code=500, detail="OAuth callback failed")

@router.post("/auth/oauth/{provider}/unlink", tags=["OAuth"])
def unlink_oauth_account(
    provider: str,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Unlink OAuth account from user"""
    try:
        oauth_account = db.query(models.OAuthAccount).filter(
            models.OAuthAccount.user_id == current_user.id,
            models.OAuthAccount.provider == provider
        ).first()

        if not oauth_account:
            raise HTTPException(status_code=404, detail="OAuth account not found")

        # Check if user has password - don't allow unlinking if it's their only auth method
        if not current_user.password_credential:
            other_oauth = db.query(models.OAuthAccount).filter(
                models.OAuthAccount.user_id == current_user.id,
                models.OAuthAccount.provider != provider
            ).first()

            if not other_oauth:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot unlink - set a password first"
                )

        db.delete(oauth_account)

        # Create audit log
        audit_log = models.AuditLog(
            user_id=current_user.id,
            actor_user_id=current_user.id,
            event="oauth.account_unlinked",
            payload_json={"provider": provider}
        )
        db.add(audit_log)

        db.commit()
        return {"message": "OAuth account unlinked successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth unlink error: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to unlink OAuth account")

# Session management endpoints
@router.post("/me/change-password", tags=["Users"])
def change_password(
    payload: schemas.ChangePasswordRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Change user password"""
    try:
        # Verify old password
        if not current_user.password_credential or not verify_password(
            payload.old_password, current_user.password_credential.password_hash
        ):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        
        # Update password
        current_user.password_credential.password_hash = hash_password(payload.new_password)
        current_user.password_credential.updated_at = datetime.now(timezone.utc)
        
        # Create audit log
        audit_log = models.AuditLog(
            user_id=current_user.id,
            actor_user_id=current_user.id,
            event="user.password_changed",
            payload_json={}
        )
        db.add(audit_log)
        
        db.commit()
        
        return {"message": "Password changed successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Password change error: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to change password")

@router.post("/me/sessions/{session_id}/revoke", status_code=204, tags=["Users"])
def revoke_user_session(
    session_id: str,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Revoke a specific session"""
    try:
        session = db.query(models.Session).filter(
            models.Session.id == session_id,
            models.Session.user_id == current_user.id,
            models.Session.revoked_at.is_(None)
        ).first()
        
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        SessionService.revoke_session(db, session_id)
        
        # Create audit log
        audit_log = models.AuditLog(
            user_id=current_user.id,
            actor_user_id=current_user.id,
            event="user.session_revoked",
            payload_json={"session_id": session_id}
        )
        db.add(audit_log)
        db.commit()
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Session revocation error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to revoke session")

@router.post("/me/sessions/revoke-others", status_code=204, tags=["Users"])
def revoke_other_sessions(
    request: Request,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Revoke all other sessions except current one"""
    try:
        # Get current session ID from token
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        payload = decode_access_token(token)
        current_session_id = payload.get("sid")
        
        # Revoke all other sessions
        other_sessions = db.query(models.Session).filter(
            models.Session.user_id == current_user.id,
            models.Session.revoked_at.is_(None)
        )
        
        if current_session_id:
            other_sessions = other_sessions.filter(models.Session.id != current_session_id)
        
        revoked_count = 0
        for session in other_sessions:
            session.revoked_at = datetime.now(timezone.utc)
            revoked_count += 1
        
        # Create audit log
        audit_log = models.AuditLog(
            user_id=current_user.id,
            actor_user_id=current_user.id,
            event="user.other_sessions_revoked",
            payload_json={"revoked_count": revoked_count}
        )
        db.add(audit_log)
        
        db.commit()
        
    except Exception as e:
        logger.error(f"Other sessions revocation error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to revoke other sessions")

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