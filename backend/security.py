
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from jose import jwt, JWTError
from passlib.context import CryptContext
from passlib.hash import argon2
import secrets
import hashlib
import hmac
import base64
from .config import settings

# Configure Argon2id password context with production-ready settings
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__memory_cost=settings.argon2_memory,
    argon2__time_cost=settings.argon2_time,
    argon2__parallelism=settings.argon2_parallelism,
    argon2__hash_len=32,
    argon2__salt_len=16,
    argon2__type="id"  # Use Argon2id variant
)

class SecurityManager:
    """Centralized security operations"""
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password using Argon2id with secure parameters"""
        if not password or len(password) < 8:
            raise ValueError("Password must be at least 8 characters long")
        return pwd_context.hash(password)
    
    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """Verify password against Argon2id hash"""
        if not password or not hashed:
            return False
        try:
            return pwd_context.verify(password, hashed)
        except Exception:
            return False
    
    @staticmethod
    def check_password_strength(password: str) -> Dict[str, Any]:
        """Check password strength and return analysis"""
        analysis = {
            "length": len(password) >= 8,
            "uppercase": any(c.isupper() for c in password),
            "lowercase": any(c.islower() for c in password),
            "digit": any(c.isdigit() for c in password),
            "special": any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in password),
            "score": 0
        }
        
        score = sum([
            analysis["length"],
            analysis["uppercase"],
            analysis["lowercase"],
            analysis["digit"],
            analysis["special"]
        ])
        
        analysis["score"] = score
        analysis["strength"] = (
            "very_weak" if score < 2 else
            "weak" if score < 3 else
            "medium" if score < 4 else
            "strong" if score < 5 else
            "very_strong"
        )
        
        return analysis

def hash_password(password: str) -> str:
    """Legacy function for backward compatibility"""
    return SecurityManager.hash_password(password)

def verify_password(password: str, hashed: str) -> bool:
    """Legacy function for backward compatibility"""
    return SecurityManager.verify_password(password, hashed)

def create_access_token(
    subject: str,
    org_id: Optional[str] = None,
    roles: List[str] = None,
    permissions: List[str] = None,
    session_id: Optional[str] = None,
    extra_claims: Optional[Dict[str, Any]] = None
) -> str:
    """Create JWT access token with comprehensive claims"""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=settings.access_token_ttl_min)
    
    # Build permissions hash for quick validation
    permissions_hash = None
    if permissions:
        perm_str = ":".join(sorted(permissions))
        permissions_hash = hashlib.sha256(perm_str.encode()).hexdigest()[:16]
    
    payload = {
        # Standard JWT claims
        "sub": subject,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "exp": expires,
        "iat": now,
        "jti": secrets.token_urlsafe(16),
        
        # Custom claims
        "scope": "access",
        "org_id": org_id,
        "roles": roles or [],
        "permissions": permissions or [],
        "permissions_hash": permissions_hash,
        "sid": session_id,
        "ver": 1,  # Token version for future compatibility
        
        # Device/security context
        "device_trust": "unknown",
        "auth_method": "password",
    }
    
    # Add any extra claims
    if extra_claims:
        payload.update(extra_claims)
    
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

def create_refresh_token() -> str:
    """Create cryptographically secure opaque refresh token"""
    return secrets.token_urlsafe(32)

def decode_access_token(token: str) -> Dict[str, Any]:
    """Decode and validate JWT access token with comprehensive validation"""
    if not token:
        raise ValueError("Token is required")
    
    try:
        payload = jwt.decode(
            token, 
            settings.jwt_secret, 
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "require_exp": True,
                "require_iat": True,
            }
        )
        
        # Validate token scope
        if payload.get("scope") != "access":
            raise JWTError("Invalid token scope")
        
        # Validate token version
        if payload.get("ver", 0) < 1:
            raise JWTError("Token version not supported")
            
        return payload
        
    except JWTError as e:
        raise ValueError(f"Invalid token: {str(e)}")
    except Exception as e:
        raise ValueError(f"Token validation failed: {str(e)}")

def hash_token(token: str) -> str:
    """Create secure hash of token for database storage"""
    if not token:
        raise ValueError("Token is required")
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

def generate_secure_token(length: int = 32) -> str:
    """Generate cryptographically secure random token"""
    return secrets.token_urlsafe(length)

def generate_verification_token() -> str:
    """Generate secure email verification token"""
    return generate_secure_token(32)

def generate_password_reset_token() -> str:
    """Generate secure password reset token"""
    return generate_secure_token(32)

def generate_device_id(user_agent: str, ip: str) -> str:
    """Generate consistent device fingerprint"""
    if not user_agent or not ip:
        return secrets.token_hex(16)
    
    device_string = f"{user_agent}:{ip}"
    return hashlib.sha256(device_string.encode()).hexdigest()[:32]

def verify_csrf_token(token: str, session_token: str) -> bool:
    """Verify CSRF token against session"""
    if not token or not session_token:
        return False
    
    expected = hmac.new(
        settings.csrf_secret.encode(),
        session_token.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(token, expected)

def generate_csrf_token(session_token: str) -> str:
    """Generate CSRF token for session"""
    return hmac.new(
        settings.csrf_secret.encode(),
        session_token.encode(),
        hashlib.sha256
    ).hexdigest()

# Rate limiting helpers
def get_rate_limit_key(identifier: str, action: str) -> str:
    """Generate rate limiting key"""
    return f"rate_limit:{action}:{identifier}"

def hash_sensitive_data(data: str, salt: Optional[str] = None) -> str:
    """Hash sensitive data with optional salt"""
    if not salt:
        salt = secrets.token_hex(16)
    
    return hashlib.pbkdf2_hmac(
        'sha256',
        data.encode('utf-8'),
        salt.encode('utf-8'),
        100000  # iterations
    ).hex()

# Backward compatibility
def decode_token(token: str, expected_scope: str = "access") -> Dict[str, Any]:
    """Backward compatibility function"""
    payload = decode_access_token(token)
    if payload.get("scope") != expected_scope:
        raise ValueError(f"Expected scope '{expected_scope}', got '{payload.get('scope')}'")
    return payload
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from passlib.context import CryptContext
from jose import jwt, JWTError
import secrets
import hashlib

from config import settings

# Password hashing
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def hash_password(password: str) -> str:
    """Hash a password using Argon2"""
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_ttl_min)
    
    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "type": "access"
    })
    
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)

def create_refresh_token(data: Dict[str, Any]) -> str:
    """Create a JWT refresh token"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days)
    
    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "type": "refresh"
    })
    
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)

def decode_access_token(token: str) -> Dict[str, Any]:
    """Decode and verify an access token"""
    try:
        payload = jwt.decode(
            token, 
            settings.jwt_secret, 
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience
        )
        
        if payload.get("type") != "access":
            raise JWTError("Invalid token type")
        
        return payload
    except JWTError:
        raise

def hash_token(token: str) -> str:
    """Hash a token for storage"""
    return hashlib.sha256(token.encode()).hexdigest()

def generate_verification_token() -> str:
    """Generate a verification token"""
    return secrets.token_urlsafe(32)

def generate_device_id() -> str:
    """Generate a device ID"""
    return secrets.token_hex(16)

class SecurityManager:
    """Security utilities"""
    
    @staticmethod
    def generate_csrf_token() -> str:
        return secrets.token_hex(32)
    
    @staticmethod
    def verify_csrf_token(token: str, expected: str) -> bool:
        return secrets.compare_digest(token, expected)
