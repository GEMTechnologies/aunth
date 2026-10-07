from fastapi import APIRouter, Depends, HTTPException, status, Header, Request, Response
from fastapi.security import OAuth2PasswordBearer, HTTPBearer
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text
from typing import Optional, List
from jose import JWTError
from datetime import datetime, timezone
import logging

from database import get_db
import schemas, models, service, security, oauth
from config import settings
from context_service import ContextService
from service import OrganizationService, AuthService, SessionService, PasswordResetService, AuditService
from security import decode_access_token
from tenant_context import (
    TenantAccessDenied,
    TenantContext,
    clear_tenant,
    for_each_tenant,
    set_tenant,
)

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
    """Get current authenticated user from JWT token.

    Also resolves the caller's tenant and publishes it on ``request.state`` so
    that :func:`get_tenant_db` can bind it before any tenant-scoped query runs.
    Resolution uses ``app.user_org_ids()`` - the SECURITY DEFINER bootstrap
    helper from migration 003 - rather than reading ``org_members`` directly,
    because ``org_members`` is protected by exactly the policy that needs the
    tenant first. See ADR-0005.
    """
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")

        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")

        # `users` and `sessions` carry no org_id and are therefore not tenant
        # tables; reading them needs no tenant scope. This runs before the
        # tenant is known, which is exactly why it is safe.
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")

        if user.status != "active":
            raise HTTPException(status_code=401, detail="User account is not active")

        request.state.user_id = user.id
        request.state.tenant_context = TenantContext.resolve(db, user.id)

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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=401, detail="Authentication failed")


def get_tenant_context(request: Request) -> TenantContext:
    """The resolved tenant for this request, or a deny-all context.

    Depends on ``get_current_user`` having run, which it has: FastAPI solves a
    dependency's sub-dependencies before its own body, and this is always
    declared alongside ``get_current_active_user``.
    """
    tenant = getattr(request.state, "tenant_context", None)
    if tenant is None:
        return TenantContext(user_id=str(getattr(request.state, "user_id", "") or ""))
    return tenant


def get_tenant_db(
    db: Session = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Yield a session with the caller's tenant bound to its connection.

    This is the dependency every authenticated, tenant-scoped endpoint must
    use instead of :func:`get_db`. It reuses ``get_db`` as a sub-dependency, so
    FastAPI's per-request dependency cache guarantees exactly one session and
    one connection per request.

    The tenant comes from the validated token, never from a path parameter or a
    query string, so editing a URL cannot reach another organisation's rows.
    Endpoints that name an organisation additionally call
    :func:`require_org_access`, and the row-level security policy still refuses
    the rows even if both of those were removed.

    A user who belongs to several organisations gets *no* tenant bound: the
    policies then deny every tenant row rather than guessing. Such endpoints
    must scope explicitly, using ``for_each_tenant`` or ``set_tenant`` after an
    explicit membership check.

    Unbinding in the ``finally`` is not optional. The settings are session-level
    so they survive the ``db.commit()`` calls this codebase already performs,
    which means a connection returned to the pool would otherwise carry the
    previous tenant into the next request.
    """
    try:
        set_tenant(db, tenant.primary_org_id, tenant.user_id)
        yield db
    finally:
        try:
            clear_tenant(db)
        except Exception:
            # The session may be closed or its transaction aborted. The pool's
            # check-in listener clears the GUCs regardless; this is the primary
            # mechanism, not the only one.
            logger.warning("could not clear tenant context on request exit",
                           exc_info=True)


def require_org_access(tenant: TenantContext, db: Session, org_id: str) -> str:
    """Authorise ``org_id`` for this caller and bind it as the current tenant.

    Two independent checks, deliberately both present:

    1. ``TenantContext.require_member`` compares the named organisation against
       the membership ids the caller proved by authenticating. This is the
       application-tier check, and it produces a clean 403 with a useful
       message.
    2. Binding the tenant is what makes row-level security enforce anything.
       Without this call the policy denies every row, so a handler that forgot
       it would fail closed rather than leak - the failure mode is an error,
       not a disclosure.

    A handler that skips step 1 still cannot read another tenant's rows; a
    handler that skips step 2 reads nothing at all.
    """
    try:
        org_id = tenant.require_member(org_id)
    except TenantAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    set_tenant(db, org_id, tenant.user_id)
    return org_id

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
    response: Response,
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

        if security.refresh_token_in_cookie():
            security.set_refresh_cookie(response, tokens.refresh_token)

        return schemas.TokenResponse(
            access_token=tokens.access_token,
            refresh_token=None if security.refresh_token_in_cookie() else tokens.refresh_token,
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
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db)
):
    """Refresh access token using refresh token.

    Accepts the token from the HttpOnly cookie or from an Authorization header,
    so browser and API clients share one endpoint.
    """
    token = security.read_refresh_token(request.cookies, authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing or invalid refresh token")

    try:
        tokens = SessionService.rotate_refresh_token(db, token)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {str(e)}")
        raise HTTPException(status_code=401, detail="Token refresh failed")

    if security.refresh_token_in_cookie():
        # Rotate the cookie too, so the credential is single-use on both paths.
        security.set_refresh_cookie(response, tokens.refresh_token)
        return schemas.TokenPair(
            access_token=tokens.access_token,
            refresh_token=None,
            token_type=tokens.token_type,
            expires_in=tokens.expires_in,
        )
    return tokens

@router.post("/auth/logout", status_code=204, tags=["Authentication"])
def logout(
    response: Response,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db)
):
    """Logout user by revoking refresh token.

    The cookie is always cleared, even when no token was presented: otherwise a
    browser whose token had already been revoked server-side would keep
    replaying a dead cookie and the UI would sit in a logged-in-looking state
    that never refreshes.
    """
    refresh_token = security.read_refresh_token(request.cookies, authorization)
    if refresh_token:
        try:
            SessionService.revoke_refresh_token(db, refresh_token)
        except Exception as e:
            logger.warning(f"Logout error: {str(e)}")
            # Don't fail logout even if token revocation fails

    if security.refresh_token_in_cookie():
        security.clear_refresh_cookie(response)
    return

@router.post("/auth/logout-all", status_code=204, tags=["Authentication"])
def logout_all_sessions(
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_tenant_db)
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
async def get_user_contexts(current_user: models.User = Depends(get_current_active_user), db: Session = Depends(get_tenant_db)):
    """Get all available contexts for the current user"""
    return ContextService.get_user_contexts(db, current_user.id)

@router.post("/me/last-context", tags=["Contexts"])
async def set_last_active_context(
    payload: schemas.SetContextRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_tenant_db)
):
    """Set user's last active context"""
    ContextService.set_last_active_context(db, current_user.id, payload.context)
    return {"message": "Context updated successfully"}

@router.get("/me/resolve-context", response_model=schemas.ContextResolutionResponse, tags=["Contexts"])
async def resolve_landing_context(
    request: Request,
    redirect_uri: Optional[str] = None,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_tenant_db)
):
    """Resolve where user should land based on context resolution algorithm"""
    host = request.headers.get("host")
    return ContextService.resolve_landing_context(db, current_user.id, host, redirect_uri)

# User profile endpoints
@router.get("/users/me", response_model=schemas.MeResponse, tags=["Users"])
def get_current_user_profile(
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_tenant_db)
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
    db: Session = Depends(get_tenant_db)
):
    """Update current user profile"""
    try:
        if payload.display_name is not None:
            current_user.display_name = payload.display_name

        if payload.locale is not None:
            current_user.locale = payload.locale

        # Create audit log
        AuditService.record(
            db,
            event="user.profile_updated",
            user_id=current_user.id,
            payload={
                "display_name": payload.display_name,
                "locale": payload.locale,
            },
        )

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
        # This endpoint intentionally keeps get_db rather than get_tenant_db:
        # there is no tenant to bind until the organisation exists. The service
        # pre-allocates the id and binds it itself immediately before the INSERT,
        # then leaves it cleared on exit.
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
    db: Session = Depends(get_tenant_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """List every organisation the caller belongs to.

    This endpoint genuinely spans tenants, so it cannot bind one. Binding the
    first membership would make row-level security hide the other organisations
    from their own member - the response would silently shrink rather than
    fail. ``for_each_tenant`` rotates the tenant once per membership and clears
    it afterwards, so each query is authorised by that tenant's own policy.
    """
    try:
        return for_each_tenant(db, tenant, lambda: [
            schemas.OrganisationResponse(
                id=membership.id,
                name=membership.name,
                slug=membership.slug,
                created_at=membership.created_at
            )
            for membership in db.query(models.Organisation).all()
        ])

    except Exception as e:
        logger.error(f"Organization listing error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list organizations")

@router.get("/orgs/{org_id}/members", tags=["Organizations"])
def list_organization_members(
    org_id: str,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_tenant_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """List members of an organization"""
    try:
        # Membership check and tenant binding in one call: without the binding
        # the RLS policy returns zero rows rather than an error, so a forgotten
        # check would look like an empty organisation.
        require_org_access(tenant, db, org_id)

        # Get all members
        members = db.query(models.OrgMember).options(
            joinedload(models.OrgMember.user).joinedload(models.User.primary_email),
            joinedload(models.OrgMember.role),
        ).filter(models.OrgMember.org_id == org_id).all()
        
        return [
            {
                "user_id": member.user_id,
                "role": member.role.key if member.role else None,
                "joined_at": member.joined_at,
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
    db: Session = Depends(get_tenant_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Invite new member to organization"""
    try:
        require_org_access(tenant, db, org_id)

        # Check if user has admin role.
        # OrgMember stores role_id, not a free-text role: `OrgMember.role.in_([...])`
        # referenced the relationship and raised AttributeError, which the
        # generic handler below turned into a 500. Join the Role row instead,
        # as the PATCH and DELETE handlers already do.
        membership = (
            db.query(models.OrgMember)
            .join(models.Role, models.Role.id == models.OrgMember.role_id)
            .filter(
                models.OrgMember.org_id == org_id,
                models.OrgMember.user_id == current_user.id,
                models.Role.key.in_(["admin", "owner"]),
            )
            .first()
        )

        if not membership:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Check if user already exists and add them directly
        existing_user = (
            db.query(models.User)
            .join(models.Email, models.Email.user_id == models.User.id)
            .filter(
                models.Email.email == payload.email.lower().strip(),
                models.Email.is_primary.is_(True),
            )
            .first()
        )
        
        if existing_user:
            # Check if already a member
            existing_member = db.query(models.OrgMember).filter(
                models.OrgMember.org_id == org_id,
                models.OrgMember.user_id == existing_user.id
            ).first()
            
            if existing_member:
                raise HTTPException(status_code=400, detail="User is already a member")
            
            # Add as member
            # OrgMember stores a role_id foreign key; the old `role=` string
            # column no longer exists, so resolve the key to a Role row owned by
            # this tenant.
            role_key = (payload.role or "member").lower()
            role_row = OrganizationService._ensure_role(
                db, role_key, role_key, org_id=org_id
            )
            new_member = models.OrgMember(
                org_id=org_id,
                user_id=existing_user.id,
                role_id=role_row.id,
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
    db: Session = Depends(get_tenant_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Update member role in organization"""
    try:
        require_org_access(tenant, db, org_id)

        # Check if current user has admin role
        admin_membership = (
            db.query(models.OrgMember)
            .join(models.Role, models.Role.id == models.OrgMember.role_id)
            .filter(
                models.OrgMember.org_id == org_id,
                models.OrgMember.user_id == current_user.id,
                models.Role.key.in_(["admin", "owner"]),
            )
            .first()
        )
        
        if not admin_membership:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Find member to update
        member = db.query(models.OrgMember).filter(
            models.OrgMember.org_id == org_id,
            models.OrgMember.user_id == user_id
        ).first()
        
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")
        
        # Update role
        new_role_key = (payload.role or "member").lower()
        member.role_id = OrganizationService._ensure_role(
            db, new_role_key, new_role_key, org_id=org_id
        ).id
        
        # Create audit log
        AuditService.record(
            db,
            event="org.member_role_updated",
            user_id=current_user.id,
            org_id=org_id,
            payload={
                "target_user_id": user_id,
                "new_role": new_role_key,
            },
        )
        
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
    db: Session = Depends(get_tenant_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Remove member from organization"""
    try:
        require_org_access(tenant, db, org_id)

        # Check if current user has admin role
        admin_membership = (
            db.query(models.OrgMember)
            .join(models.Role, models.Role.id == models.OrgMember.role_id)
            .filter(
                models.OrgMember.org_id == org_id,
                models.OrgMember.user_id == current_user.id,
                models.Role.key.in_(["admin", "owner"]),
            )
            .first()
        )
        
        if not admin_membership:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Find member to remove
        member = db.query(models.OrgMember).filter(
            models.OrgMember.org_id == org_id,
            models.OrgMember.user_id == user_id
        ).first()
        
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")
        
        # Don't allow removing the last owner
        member_role_key = member.role.key if member.role else None
        if member_role_key == "owner":
            owner_count = (
                db.query(models.OrgMember)
                .join(models.Role, models.Role.id == models.OrgMember.role_id)
                .filter(
                    models.OrgMember.org_id == org_id,
                    models.Role.key == "owner",
                )
                .count()
            )
            
            if owner_count <= 1:
                raise HTTPException(
                    status_code=400, 
                    detail="Cannot remove the last owner"
                )
        
        db.delete(member)
        
        # Create audit log
        AuditService.record(
            db,
            event="org.member_removed",
            user_id=current_user.id,
            org_id=org_id,
            payload={
                "removed_user_id": user_id,
                "removed_role": member_role_key,
            },
        )
        
        db.commit()
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Member removal error: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to remove member")

# OAuth/Social Login endpoints
#
# These two routes used to live here as well, calling a bare ``oauth_service``
# name that does not exist in this module's namespace - so the handler raised
# NameError and the broad ``except Exception`` turned it into a 500.
#
# Wiring the name up would have been the wrong repair. ``main.py`` mounts *both*
# ``router`` and ``oauth.router`` under /api/v1, so /auth/oauth/{provider}/
# {authorize,callback} were each registered twice, and because ``router`` is
# included first its copies won the match. The copies here were not equivalent:
#
#   * This module's callback returned a ``schemas.TokenResponse`` - access token
#     and refresh token in a JSON body, from a GET, on a URL the browser visited
#     by redirect. That puts credentials in the URL bar, in browser history, and
#     in any proxy log that records the query string, which is precisely what the
#     one-time-code design exists to prevent.
#   * ``oauth.handle_oauth_callback`` issues a single-use code and redirects with
#     ``?code=`` only; ``POST /auth/oauth/exchange`` trades that code for the
#     tokens, burning it on first use. Covered by ``tests/test_oauth_token_leak.py``.
#
# So the duplicates are removed rather than repaired. oauth.py owns this flow.
@router.post("/auth/oauth/{provider}/unlink", tags=["OAuth"])
def unlink_oauth_account(
    provider: str,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_tenant_db)
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
        AuditService.record(
            db,
            event="oauth.account_unlinked",
            user_id=current_user.id,
            payload={"provider": provider},
        )

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
    db: Session = Depends(get_tenant_db)
):
    """Change user password"""
    try:
        # Verify old password. These are ``security`` module functions, not
        # bare names: this module imports the module, never the two functions,
        # so the bare spelling raised NameError and the broad handler below
        # reported it as a 500 "Failed to change password".
        if not current_user.password_credential or not security.verify_password(
            payload.old_password, current_user.password_credential.password_hash
        ):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        
        # Update password
        current_user.password_credential.password_hash = security.hash_password(payload.new_password)
        current_user.password_credential.updated_at = datetime.now(timezone.utc)
        
        # Create audit log
        AuditService.record(
            db,
            event="user.password_changed",
            user_id=current_user.id,
            payload={},
        )
        
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
    db: Session = Depends(get_tenant_db)
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
        AuditService.record(
            db,
            event="user.session_revoked",
            user_id=current_user.id,
            payload={"session_id": session_id},
        )
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
    db: Session = Depends(get_tenant_db)
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
        AuditService.record(
            db,
            event="user.other_sessions_revoked",
            user_id=current_user.id,
            payload={"revoked_count": revoked_count},
        )
        
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
        db.execute(text("SELECT 1"))
        return {
            "status": "healthy",
            "service": "granada-auth",
            "version": settings.api_version
        }
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        raise HTTPException(status_code=503, detail="Service unhealthy")

# Legacy compatibility endpoints
#
# This section used to also carry `register_legacy` (POST /auth/register) and
# `get_me_legacy` (GET /users/me). Both were unreachable: Starlette matches the
# first registered route, and the modern handlers for both paths are declared
# earlier in this same file. So they shadowed nothing and served nothing.
#
# They were not harmless leftovers. Each returned `schemas.UserRead` while the
# handler actually serving that path returns `UserResponse` - so PATCH
# /users/me and GET /users/me would have disagreed about the shape of the same
# resource had the order ever changed. The frontend calls GET /users/me
# (SecurityPage.tsx) expecting the modern shape.
#
# `tests/test_router_integrity.py::test_no_path_is_registered_twice` now fails if
# a duplicate registration reappears, which is what allowed this to go unnoticed.
# `create_org_legacy` below does not collide with anything and stays.
@router.post("/orgs", response_model=schemas.OrgRead, tags=["Organizations", "Legacy"])
def create_org_legacy(
    payload: schemas.OrgCreate,
    db: Session = Depends(get_db),
    current: models.User = Depends(get_current_user)
):
    """Legacy organization creation endpoint"""
    org = OrganizationService.create_organization(db, payload.name, current)
    return schemas.OrgRead(id=org.id, name=org.name)
