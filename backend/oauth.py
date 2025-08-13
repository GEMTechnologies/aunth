
from typing import Optional, Dict, Any, List
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
import httpx
import secrets
from datetime import datetime, timezone, timedelta
import logging

from . import models, schemas
from .config import settings
from .security import generate_secure_token

logger = logging.getLogger(__name__)

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
                raise HTTPException(
                    status_code=400, 
                    detail="Failed to exchange authorization code"
                )
            
            return response.json()
    
    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """Get user info from Google"""
        headers = {"Authorization": f"Bearer {access_token}"}
        
        async with httpx.AsyncClient() as client:
            response = await client.get(self.user_info_url, headers=headers)
            
            if response.status_code != 200:
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
                raise HTTPException(
                    status_code=400, 
                    detail="Failed to exchange authorization code"
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
            
            return user_data

class OAuthService:
    """OAuth authentication service"""
    
    def __init__(self):
        self.providers = {
            "google": GoogleOAuthProvider(
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                redirect_uri=f"{settings.api_url}/auth/oauth/google/callback"
            ),
            "github": GitHubOAuthProvider(
                client_id=settings.github_client_id,
                client_secret=settings.github_client_secret,
                redirect_uri=f"{settings.api_url}/auth/oauth/github/callback"
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
    ) -> models.User:
        """Handle OAuth callback and create/login user"""
        
        # Validate state
        oauth_state = db.query(models.OAuthState).filter(
            models.OAuthState.state == state,
            models.OAuthState.provider == provider_name,
            models.OAuthState.used_at.is_(None),
            models.OAuthState.expires_at > datetime.now(timezone.utc)
        ).first()
        
        if not oauth_state:
            raise HTTPException(status_code=400, detail="Invalid or expired state")
        
        # Mark state as used
        oauth_state.used_at = datetime.now(timezone.utc)
        
        provider = self.get_provider(provider_name)
        
        # Exchange code for tokens
        token_data = await provider.exchange_code(code, state)
        access_token = token_data.get("access_token")
        
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to get access token")
        
        # Get user info
        user_info = await provider.get_user_info(access_token)
        
        # Find or create user
        user = await self._find_or_create_oauth_user(
            db, provider_name, user_info, token_data
        )
        
        db.commit()
        return user
    
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
            raise HTTPException(
                status_code=400, 
                detail="Email not provided by OAuth provider"
            )
        
        # Check for existing OAuth connection
        oauth_account = db.query(models.OAuthAccount).filter(
            models.OAuthAccount.provider == provider_name,
            models.OAuthAccount.provider_user_id == provider_user_id
        ).first()
        
        if oauth_account:
            # Update tokens
            oauth_account.access_token = token_data.get("access_token")
            oauth_account.refresh_token = token_data.get("refresh_token")
            oauth_account.token_expires_at = self._calculate_token_expiry(token_data)
            oauth_account.last_login_at = datetime.now(timezone.utc)
            
            return oauth_account.user
        
        # Check for existing user by email
        email_obj = db.query(models.Email).filter(
            models.Email.email == email.lower().strip()
        ).first()
        
        if email_obj:
            # Link OAuth account to existing user
            user = email_obj.user
        else:
            # Create new user
            user = models.User(
                display_name=user_info.get("name") or user_info.get("login") or email.split("@")[0],
                locale="en",
                status="active"
            )
            db.add(user)
            db.flush()
            
            # Create email
            email_obj = models.Email(
                user_id=user.id,
                email=email.lower().strip(),
                is_verified=True,  # OAuth emails are pre-verified
                is_primary=True
            )
            db.add(email_obj)
            db.flush()
            
            user.primary_email_id = email_obj.id
        
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
        audit_log = models.AuditLog(
            user_id=user.id,
            event="oauth.account_linked",
            payload_json={
                "provider": provider_name,
                "provider_user_id": provider_user_id,
                "email": email
            }
        )
        db.add(audit_log)
        
        return user
    
    def _calculate_token_expiry(self, token_data: Dict[str, Any]) -> Optional[datetime]:
        """Calculate token expiry from OAuth response"""
        expires_in = token_data.get("expires_in")
        if expires_in:
            return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        return None

# Global OAuth service instance
oauth_service = OAuthService()
