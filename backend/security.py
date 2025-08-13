
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from jose import jwt, JWTError
from passlib.context import CryptContext
from passlib.hash import argon2
import secrets
import hashlib
from .config import settings

# Configure Argon2id password context
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__memory_cost=settings.argon2_memory,
    argon2__time_cost=settings.argon2_time,
    argon2__parallelism=settings.argon2_parallelism,
)

def hash_password(password: str) -> str:
    """Hash password using Argon2id"""
    return pwd_context.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against Argon2id hash"""
    return pwd_context.verify(password, hashed)

def create_access_token(
    subject: str,
    org_id: Optional[str] = None,
    roles: List[str] = None,
    permissions_hash: Optional[str] = None,
    session_id: Optional[str] = None
) -> str:
    """Create short-lived access JWT token"""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=settings.access_token_ttl_min)
    
    payload = {
        "sub": subject,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "exp": expires,
        "iat": now,
        "scope": "access",
        "org_id": org_id,
        "roles": roles or [],
        "permissions_hash": permissions_hash,
        "sid": session_id,
        "ver": 1  # Policy version
    }
    
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

def create_refresh_token() -> str:
    """Create opaque refresh token"""
    return secrets.token_urlsafe(32)

def decode_access_token(token: str) -> Dict[str, Any]:
    """Decode and validate access token"""
    try:
        payload = jwt.decode(
            token, 
            settings.jwt_secret, 
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer
        )
        
        if payload.get("scope") != "access":
            raise JWTError("Invalid token scope")
            
        return payload
    except JWTError as e:
        raise ValueError(f"Invalid token: {str(e)}")

def hash_token(token: str) -> str:
    """Hash token for secure storage"""
    return hashlib.sha256(token.encode()).hexdigest()

def generate_verification_token() -> str:
    """Generate secure verification token"""
    return secrets.token_urlsafe(32)

def generate_device_id(user_agent: str, ip: str) -> str:
    """Generate device fingerprint"""
    device_string = f"{user_agent}:{ip}"
    return hashlib.sha256(device_string.encode()).hexdigest()[:16]

# Backward compatibility alias
def decode_token(token: str, expected_scope: str = "access") -> Dict[str, Any]:
    """Backward compatibility function"""
    return decode_access_token(token)
