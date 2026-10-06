from typing import Optional, Dict, Any, List
from fastapi import HTTPException, status, Depends, APIRouter
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import httpx
import secrets
from datetime import datetime, timezone, timedelta
import logging

import models, schemas
from config import settings
from security import generate_secure_token, create_access_token, create_refresh_token, hash_token
from database import get_db
from models import User, OAuthState, OAuthAccount, AuditLog, OAuthAuthCode

logger = logging.getLogger(__name__)

router = APIRouter()

# The OAuth callback used to read ``FRONTEND_URL``. The Settings
# field is ``frontend_url``; the capitalised name raises AttributeError, which
# meant every error path below crashed while reporting the original error.
FRONTEND_URL = settings.frontend_url

# A redirect code is a credential. It is short-lived and single-use.
AUTH_CODE_TTL_SECONDS = 120


def _frontend_redirect(**params: Any) -> RedirectResponse:
    """Redirect back to the SPA carrying only non-secret parameters."""
    from urllib.parse import urlencode

    clean = {k: v for k, v in params.items() if v is not None}
    return RedirectResponse(url=f"{FRONTEND_URL}?{urlencode(clean)}")


def issue_auth_code(
    db: Session,
    user_id: str,
    session_id: Optional[str] = None,
    redirect_to: Optional[str] = None,
) -> str:
    """Mint a single-use code the SPA exchanges for tokens over POST.

    Returning the raw access and refresh tokens in a redirect URL puts them
    into browser history, the Referer header, proxy logs and provider logs.
    The URL therefore carries only this code, which is useless after one use
    or after two minutes.
    """
    code = secrets.token_urlsafe(32)
    db.add(
        models.OAuthAuthCode(
            code_hash=hash_token(code),
            user_id=user_id,
            session_id=session_id,
            redirect_to=redirect_to,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=AUTH_CODE_TTL_SECONDS),
        )
    )
    db.commit()
    return code

class OAuthProvider:
    """Base OAuth provider class"""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    async def get_authorization_url(self, state: str) -> str:
        """Generate OAuth authorization URL"""
        raise NotImplementedError

    async def exchange_code(self, code: str, state: str) -> Dict[str, Any]:
        """Exchange authorization code for tokens"""
        raise NotImplementedError

    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """Get user info from OAuth provider"""
        raise NotImplementedError

class GoogleOAuthProvider(OAuthProvider):
    """Google OAuth 2.0 provider"""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        super().__init__(client_id, client_secret, redirect_uri)
        self.auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
        self.token_url = "https://oauth2.googleapis.com/token"
        self.user_info_url = "https://www.googleapis.com/oauth2/v2/userinfo"
        self.scope = "openid email profile"

    async def get_authorization_url(self, state: str) -> str:
        """Generate Google OAuth authorization URL"""
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "response_type": "code",
            "state": state,
            "access_type": "offline",
            "prompt": "consent"
        }

        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        return f"{self.auth_url}?{query_string}"

    async def exchange_code(self, code: str, state: str) -> Dict[str, Any]:
        """Exchange authorization code for Google tokens"""
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(self.token_url, data=data)

            if response.status_code != 200:
                logger.error(f"Google token exchange failed: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=400,
                    detail="Failed to exchange authorization code with Google"
                )

            return response.json()

    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """Get user info from Google"""
        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient() as client:
            response = await client.get(self.user_info_url, headers=headers)

            if response.status_code != 200:
                logger.error(f"Google user info retrieval failed: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=400,
                    detail="Failed to get user info from Google"
                )

            return response.json()

class GitHubOAuthProvider(OAuthProvider):
    """GitHub OAuth provider"""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        super().__init__(client_id, client_secret, redirect_uri)
        self.auth_url = "https://github.com/login/oauth/authorize"
        self.token_url = "https://github.com/login/oauth/access_token"
        self.user_info_url = "https://api.github.com/user"
        self.user_email_url = "https://api.github.com/user/emails"
        self.scope = "user:email"

    async def get_authorization_url(self, state: str) -> str:
        """Generate GitHub OAuth authorization URL"""
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "state": state,
            "allow_signup": "true"
        }

        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        return f"{self.auth_url}?{query_string}"

    async def exchange_code(self, code: str, state: str) -> Dict[str, Any]:
        """Exchange authorization code for GitHub tokens"""
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code
        }

        headers = {"Accept": "application/json"}

        async with httpx.AsyncClient() as client:
            response = await client.post(self.token_url, data=data, headers=headers)

            if response.status_code != 200:
                logger.error(f"GitHub token exchange failed: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=400,
                    detail="Failed to exchange authorization code with GitHub"
                )

            return response.json()

    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """Get user info from GitHub"""
        headers = {
            "Authorization": f"token {access_token}",
            "Accept": "application/vnd.github.v3+json"
        }

        async with httpx.AsyncClient() as client:
            # Get user profile
            user_response = await client.get(self.user_info_url, headers=headers)
            if user_response.status_code != 200:
                logger.error(f"GitHub user profile retrieval failed: {user_response.status_code} - {user_response.text}")
                raise HTTPException(
                    status_code=400,
                    detail="Failed to get user info from GitHub"
                )

            user_data = user_response.json()

            # Get user emails
            email_response = await client.get(self.user_email_url, headers=headers)
            if email_response.status_code == 200:
                emails = email_response.json()
                primary_email = next(
                    (email["email"] for email in emails if email["primary"]),
                    user_data.get("email")
                )
                user_data["email"] = primary_email
            elif user_data.get("email") is None:
                 logger.error(f"GitHub email retrieval failed and no primary email found in profile: {email_response.status_code} - {email_response.text}")
                 raise HTTPException(
                    status_code=400,
                    detail="Failed to retrieve primary email from GitHub"
                 )

            return user_data

class OAuthService:
    """OAuth authentication service"""

    def __init__(self):
        self.providers = {
            "google": GoogleOAuthProvider(
                client_id=getattr(settings, 'google_client_id', ''),
                client_secret=getattr(settings, 'google_client_secret', ''),
                redirect_uri=f"{getattr(settings, 'api_url', 'http://localhost:8000')}/api/auth/oauth/google/callback"
            ),
            "github": GitHubOAuthProvider(
                client_id=getattr(settings, 'github_client_id', ''),
                client_secret=getattr(settings, 'github_client_secret', ''),
                redirect_uri=f"{getattr(settings, 'api_url', 'http://localhost:8000')}/api/auth/oauth/github/callback"
            )
        }

    def get_provider(self, provider_name: str) -> OAuthProvider:
        """Get OAuth provider by name"""
        if provider_name not in self.providers:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported OAuth provider: {provider_name}"
            )
        return self.providers[provider_name]

    async def initiate_oauth_flow(self, provider_name: str, db: Session) -> Dict[str, str]:
        """Initiate OAuth flow"""
        provider = self.get_provider(provider_name)

        # Generate state token
        state = generate_secure_token(32)

        # Store state in database for validation
        oauth_state = models.OAuthState(
            state=state,
            provider=provider_name,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)
        )
        db.add(oauth_state)
        db.commit()

        # Get authorization URL
        auth_url = await provider.get_authorization_url(state)

        return {
            "authorization_url": auth_url,
            "state": state
        }

    async def handle_oauth_callback(
        self,
        provider_name: str,
        code: str,
        state: str,
        db: Session
    ) -> RedirectResponse:
        """Handle OAuth callback and create/login user"""

        # Validate state
        oauth_state = db.query(models.OAuthState).filter(
            models.OAuthState.state == state,
            models.OAuthState.provider == provider_name,
            models.OAuthState.used_at.is_(None),
            models.OAuthState.expires_at > datetime.now(timezone.utc)
        ).first()

        if not oauth_state:
            logger.warning(f"Invalid or expired state received for provider: {provider_name}, state: {state}")
            return RedirectResponse(url=f"{FRONTEND_URL}?error=Invalid or expired state")

        # Mark state as used
        oauth_state.used_at = datetime.now(timezone.utc)
        db.commit() # Commit state usage immediately

        provider = self.get_provider(provider_name)

        # Exchange code for tokens
        try:
            token_data = await provider.exchange_code(code, state)
        except HTTPException as e:
            logger.error(f"Error during token exchange for {provider_name}: {e.detail}")
            return RedirectResponse(url=f"{FRONTEND_URL}?error={e.detail}")
        except Exception as e:
            logger.error(f"Unexpected error during token exchange for {provider_name}: {e}")
            return RedirectResponse(url=f"{FRONTEND_URL}?error=An unexpected error occurred during authentication.")


        access_token = token_data.get("access_token")

        if not access_token:
            logger.error(f"Access token not found in response from {provider_name}: {token_data}")
            return RedirectResponse(url=f"{FRONTEND_URL}?error=Failed to retrieve access token.")

        # Get user info
        try:
            user_info = await provider.get_user_info(access_token)
        except HTTPException as e:
            logger.error(f"Error retrieving user info from {provider_name}: {e.detail}")
            return RedirectResponse(url=f"{FRONTEND_URL}?error={e.detail}")
        except Exception as e:
            logger.error(f"Unexpected error retrieving user info from {provider_name}: {e}")
            return RedirectResponse(url=f"{FRONTEND_URL}?error=An unexpected error occurred while fetching user information.")


        # Find or create user
        user = await self._find_or_create_oauth_user(
            db, provider_name, user_info, token_data
        )

        db.commit() # Commit user creation/update

        # Create a real session so the refresh token is stored, revocable and
        # replay-detectable, then hand the browser a one-time code.
        from service import SessionService

        session_record = SessionService.create_session(
            db, user, user_agent=self._user_agent
        )
        SessionService.issue_tokens(db, user, session_record)
        db.commit()

        code = issue_auth_code(db, str(user.id), session_record.id)

        # The URL carries a single-use code, never a credential.
        return _frontend_redirect(code=code)

    async def _find_or_create_oauth_user(
        self,
        db: Session,
        provider_name: str,
        user_info: Dict[str, Any],
        token_data: Dict[str, Any]
    ) -> models.User:
        """Find existing user or create new one from OAuth data"""

        provider_user_id = str(user_info.get("id"))
        email = user_info.get("email")

        if not email:
            logger.error(f"Email missing from user_info for provider {provider_name}: {user_info}")
            raise HTTPException(
                status_code=400,
                detail="Email not provided by OAuth provider"
            )

        new_user_created = False

        # Check for existing OAuth connection
        oauth_account = db.query(models.OAuthAccount).filter(
            models.OAuthAccount.provider == provider_name,
            models.OAuthAccount.provider_user_id == provider_user_id
        ).first()

        if oauth_account:
            # Update tokens and provider data
            oauth_account.access_token = token_data.get("access_token")
            oauth_account.refresh_token = token_data.get("refresh_token")
            oauth_account.token_expires_at = self._calculate_token_expiry(token_data)
            oauth_account.provider_data = user_info # Update provider data
            oauth_account.last_login_at = datetime.now(timezone.utc)
            logger.info(f"Updated existing OAuth account for user ID: {oauth_account.user_id}, provider: {provider_name}")
            return oauth_account.user

        # Check for existing user by email
        email_obj = db.query(models.Email).filter(
            models.Email.email == email.lower().strip()
        ).first()

        if email_obj:
            # Link OAuth account to existing user
            user = email_obj.user
            logger.info(f"Linking OAuth account to existing user ID: {user.id} via email: {email}")
        else:
            # Create new user
            new_user_created = True
            display_name = user_info.get("name") or user_info.get("login") or email.split("@")[0]
            user = models.User(
                display_name=display_name,
                locale="en", # Default locale, can be updated later if provider sends it
                status="active"
            )
            db.add(user)
            db.flush() # Flush to get user.id before creating email and OAuthAccount

            logger.info(f"Created new user with ID: {user.id}, display name: {display_name}")

            # Create email
            email_obj = models.Email(
                user_id=user.id,
                email=email.lower().strip(),
                is_verified=True,  # OAuth emails are generally considered pre-verified
                is_primary=True
            )
            db.add(email_obj)
            db.flush() # Flush to get email_obj.id

            user.primary_email_id = email_obj.id # Set primary email

        # Create OAuth account link
        oauth_account = models.OAuthAccount(
            user_id=user.id,
            provider=provider_name,
            provider_user_id=provider_user_id,
            access_token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            token_expires_at=self._calculate_token_expiry(token_data),
            provider_data=user_info
        )
        db.add(oauth_account)

        # Create audit log
        from service import AuditService

        AuditService.record(
            db,
            event="oauth.account_linked",
            user_id=user.id,
            payload={
                "provider": provider_name,
                "provider_user_id": provider_user_id,
                "email": email,
                "new_user_created": bool(new_user_created),
            },
        )
        logger.info(f"Created OAuth account link for user ID: {user.id}, provider: {provider_name}, provider user ID: {provider_user_id}")

        return user

    def _calculate_token_expiry(self, token_data: Dict[str, Any]) -> Optional[datetime]:
        """Calculate token expiry from OAuth response"""
        expires_in = token_data.get("expires_in")
        if expires_in:
            try:
                return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            except ValueError:
                logger.warning(f"Could not convert expires_in to int: {expires_in}")
        return None

# Global OAuth service instance
oauth_service = OAuthService()

@router.get("/auth/oauth/{provider}/authorize")
async def initiate_oauth(provider: str, db: Session = Depends(get_db)):
    """Initiate OAuth flow for a given provider"""
    try:
        result = await oauth_service.initiate_oauth_flow(provider, db)
        return result
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error initiating OAuth flow for {provider}: {e}")
        raise HTTPException(status_code=500, detail="An internal error occurred during OAuth initiation.")


@router.get("/auth/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Handle OAuth callback and create/login user"""
    if error:
        logger.error(f"OAuth callback error for {provider}: {error}")
        return RedirectResponse(url=f"{FRONTEND_URL}?error={error}")

    if not code or not state:
        logger.error(f"Missing code or state in OAuth callback for {provider}")
        return RedirectResponse(url=f"{FRONTEND_URL}?error=Authentication parameters missing")

    try:
        return await oauth_service.handle_oauth_callback(provider, code, state, db)
    except HTTPException as e:
        logger.error(f"HTTP Exception during OAuth callback for {provider}: {e.detail}")
        return RedirectResponse(url=f"{FRONTEND_URL}?error={e.detail}")
    except Exception as e:
        logger.exception(f"Unexpected error during OAuth callback for {provider}") # Log the full traceback
        return RedirectResponse(url=f"{FRONTEND_URL}?error=An unexpected error occurred during authentication.")


class CodeExchangeRequest(BaseModel):
    code: str = Field(..., min_length=10, max_length=256)


@router.post("/auth/oauth/exchange")
async def exchange_oauth_code(
    payload: CodeExchangeRequest,
    db: Session = Depends(get_db),
):
    """Trade a one-time redirect code for the tokens it stands for.

    Single-use and short-lived: the row is burned on the first successful
    exchange, so a code captured from history or a log cannot be replayed.
    """
    from service import SessionService

    record = (
        db.query(models.OAuthAuthCode)
        .filter(models.OAuthAuthCode.code_hash == hash_token(payload.code))
        .one_or_none()
    )

    now = datetime.now(timezone.utc)

    def _invalid() -> HTTPException:
        # One message for every failure mode, so the endpoint does not
        # disclose whether a code ever existed.
        return HTTPException(status_code=401, detail="Invalid or expired code")

    if record is None or record.used_at is not None:
        raise _invalid()

    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        raise _invalid()

    user = db.query(models.User).filter(models.User.id == record.user_id).one_or_none()
    if user is None or user.status != "active":
        # Suspended or deleted accounts must not complete a sign-in that
        # started while they were still active.
        raise _invalid()

    # Burn the code before handing out anything, so a concurrent replay of
    # the same code cannot race two token pairs into existence.
    record.used_at = now
    db.commit()

    session_record = SessionService.create_session(db, user)
    pair = SessionService.issue_tokens(db, user, session_record)
    db.commit()

    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
    }