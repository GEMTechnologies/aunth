"""Granada Authentication Service - security primitives.

Phase 1 remediation (ADR-0002).

This module previously contained TEN duplicated top-level symbols. Python
executes a module top-to-bottom, so the *second* definition of every symbol
silently replaced the first. The surviving definitions were the weaker ones:

  * the Argon2id context configured from settings was replaced by a default
    CryptContext, discarding the configured cost parameters;
  * ``create_access_token`` lost its tenant-aware signature and silently began
    minting tokens with no ``org_id`` and no ``roles``;
  * ``SecurityManager`` lost ``hash_password``/``verify_password``;
  * ``secrets.compare_digest`` was called, but that symbol lives in ``hmac``.

There is now exactly one definition of each symbol in this module. Adding a
second one will silently reintroduce the bug that ADR-0002 exists to prevent.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from jose import JWTError, jwt
from passlib.context import CryptContext

from config import settings

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

#: Argon2id with cost parameters supplied by configuration. The previous
#: duplicate definition replaced this with a bare ``CryptContext(...)`` that
#: ignored ``settings.argon2_*``, so the configured cost was never applied.
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__memory_cost=settings.argon2_memory,
    argon2__time_cost=settings.argon2_time,
    argon2__parallelism=settings.argon2_parallelism,
    argon2__hash_len=32,
    argon2__salt_len=16,
    argon2__type="id",  # Argon2id
)

MIN_PASSWORD_LENGTH = 8


class TenantScopeError(Exception):
    """Raised when a request cannot be attributed to exactly one tenant.

    "Tenant unknown" is always a DENY. It must never fall back to a default
    organisation, because that is a cross-tenant read.
    """


class SecurityManager:
    """Centralised security operations."""

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using Argon2id with configured cost."""
        if not password or len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters long"
            )
        return pwd_context.hash(password)

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """Verify a password against an Argon2id hash."""
        if not password or not hashed:
            return False
        try:
            return pwd_context.verify(password, hashed)
        except Exception:
            return False

    @staticmethod
    def check_password_strength(password: str) -> Dict[str, Any]:
        """Return a structured password strength assessment."""
        analysis = {
            "length": len(password or "") >= MIN_PASSWORD_LENGTH,
            "uppercase": any(c.isupper() for c in (password or "")),
            "lowercase": any(c.islower() for c in (password or "")),
            "digit": any(c.isdigit() for c in (password or "")),
            "special": any(
                c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in (password or "")
            ),
        }
        analysis["score"] = sum(1 for v in analysis.values() if v)
        score = analysis["score"]
        analysis["strength"] = (
            "very_weak" if score < 2
            else "weak" if score < 3
            else "medium" if score < 4
            else "strong" if score < 5
            else "very_strong"
        )
        return analysis

    @staticmethod
    def generate_csrf_token() -> str:
        return secrets.token_hex(32)

    @staticmethod
    def verify_csrf_token(token: str, expected: str) -> bool:
        # NOTE: the previous version called ``secrets.compare_digest``, which
        # does not exist -- the function lives in ``hmac``. This raises
        # AttributeError on every CSRF check.
        if not token or not expected:
            return False
        return hmac.compare_digest(token, expected)


# ---------------------------------------------------------------------------
# Module-level wrappers retained for existing call sites.
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return SecurityManager.hash_password(password)


def verify_password(password: str, hashed: str) -> bool:
    return SecurityManager.verify_password(password, hashed)


def check_password_strength(password: str) -> Dict[str, Any]:
    return SecurityManager.check_password_strength(password)


# ---------------------------------------------------------------------------
# Access tokens (tenant-scoped)
# ---------------------------------------------------------------------------

def _accepted_audiences() -> List[str]:
    audience = settings.jwt_audience
    if isinstance(audience, (list, tuple, set)):
        return [str(a) for a in audience]
    return [str(audience)]


def _validate_audience(payload: Dict[str, Any]) -> None:
    """Check the token audience against the configured accepted set.

    ``settings.jwt_audience`` is a list of audiences this deployment accepts;
    a token is valid if its ``aud`` claim is any one of them.
    """
    accepted = _accepted_audiences()
    claimed = payload.get("aud")
    if claimed is None:
        raise ValueError("Token is missing an audience claim")

    claimed_values = claimed if isinstance(claimed, (list, tuple)) else [claimed]
    if not set(str(c) for c in claimed_values).intersection(accepted):
        raise ValueError(
            f"Token audience {claimed!r} is not accepted by this service"
        )


def _primary_audience() -> str:
    """Return the audience claim value to embed in a token.

    ``settings.jwt_audience`` may be a list of accepted audiences, but a JWT
    ``aud`` claim must be a single string; python-jose rejects a list-valued
    ``aud``. Tokens therefore carry the first configured audience and are
    validated against the whole accepted set.
    """
    audience = settings.jwt_audience
    if isinstance(audience, (list, tuple, set)):
        if not audience:
            raise ValueError("settings.jwt_audience must not be empty")
        return str(next(iter(audience)))
    return str(audience)


def _permissions_hash(permissions: Optional[Sequence[str]]) -> Optional[str]:
    if not permissions:
        return None
    joined = ":".join(sorted(str(p) for p in permissions))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def create_access_token(
    subject: str,
    org_id: Optional[str] = None,
    roles: Optional[Sequence[str]] = None,
    permissions: Optional[Sequence[str]] = None,
    session_id: Optional[str] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
    *,
    org_ids: Optional[Sequence[str]] = None,
    auth_method: str = "password",
    device_trust: str = "unknown",
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Mint a signed JWT access token carrying tenant and role claims.

    ``org_id`` is recorded even when it is ``None``. A caller that needs tenant
    isolation MUST call :func:`resolve_tenant`, which denies rather than
    defaulting. Emitting a token without a tenant is not the same as granting
    access to a default tenant, and the two must not be conflated.
    """
    if not subject:
        raise ValueError("subject is required to mint an access token")

    now = datetime.now(timezone.utc)
    expires = now + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.access_token_ttl_min)
    )

    roles = list(roles or [])
    payload: Dict[str, Any] = {
        # Registered claims
        "sub": str(subject),
        "iss": settings.jwt_issuer,
        "aud": _primary_audience(),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": secrets.token_urlsafe(16),
        # Token shape
        "type": "access",
        "ver": 1,
        # Tenant + authorisation
        "org_id": org_id,
        "org_ids": list(org_ids or ([org_id] if org_id else [])),
        "roles": roles,
        "permissions": list(permissions or []),
        "permissions_hash": _permissions_hash(permissions),
        "sid": session_id,
        # Provenance
        "auth_method": auth_method,
        "device_trust": device_trust,
    }

    if extra_claims:
        # Reserved claims may not be overridden by callers.
        reserved = {
            "sub", "iss", "aud", "iat", "nbf", "exp",
            "type", "ver", "org_id", "org_ids", "roles",
            "permissions", "permissions_hash", "sid",
        }
        collisions = reserved.intersection(extra_claims)
        if collisions:
            raise ValueError(
                "extra_claims may not override reserved claims: "
                + ", ".join(sorted(collisions))
            )
        payload.update(extra_claims)

    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Dict[str, Any]:
    """Decode and validate an access token.

    Validates signature, expiry, issuer and audience, and rejects tokens that
    are not access tokens or were minted without a recognised token version.
    """
    if not token:
        raise ValueError("Token is required")

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                # Audience is validated below against the whole accepted set;
                # python-jose only accepts a single string here.
                "verify_aud": False,
                "verify_iss": True,
                "require_exp": True,
                "require_iat": True,
            },
        )
    except JWTError as exc:
        raise ValueError(f"Invalid token: {exc}") from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError(f"Token validation failed: {exc}") from exc

    _validate_audience(payload)

    if payload.get("type") != "access":
        raise ValueError("Invalid token type")
    if payload.get("ver", 0) < 1:
        raise ValueError("Token version not supported")
    if not payload.get("sub"):
        raise ValueError("Token is missing a subject")

    return payload


def resolve_tenant(payload: Dict[str, Any], required: bool = True) -> Optional[str]:
    """Return the tenant for an already-validated token payload.

    Raises :class:`TenantScopeError` when the token carries no organisation and
    ``required`` is true. An absent tenant is a DENY, never a default tenant.
    """
    org_id = payload.get("org_id")
    if org_id:
        return str(org_id)
    if required:
        raise TenantScopeError(
            "Token carries no tenant scope; tenant-unknown requests are denied"
        )
    return None


def require_tenant(payload: Dict[str, Any], org_id: str) -> str:
    """Assert the token is scoped to exactly ``org_id`` and return it.

    ``resolve_tenant`` answers "does this token carry a tenant". This answers
    the question every tenant-scoped endpoint actually needs to ask: is this
    token scoped to the organisation whose data was requested. A token from
    another organisation, or one carrying no tenant at all, is denied.
    """
    if not org_id:
        raise TenantScopeError(
            "No organisation was specified for a tenant-scoped operation"
        )
    scoped = resolve_tenant(payload, required=True)
    if str(scoped) != str(org_id):
        raise TenantScopeError(f"Token is not scoped to organisation {org_id}")
    return str(scoped)


# ---------------------------------------------------------------------------
# Refresh tokens (opaque, rotated, reuse-detected)
# ---------------------------------------------------------------------------

def create_refresh_token(data: Optional[Dict[str, Any]] = None) -> str:
    """Create an opaque refresh token.

    Refresh tokens are opaque random strings, not JWTs: only their SHA-256
    digest is stored, which lets the service detect replay of a rotated token.
    The optional ``data`` argument is accepted for call-site compatibility and
    is deliberately NOT embedded in the token.
    """
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """Hash an opaque token for database storage."""
    if not token:
        raise ValueError("Token is required")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Opaque tokens, CSRF, rate limiting
# ---------------------------------------------------------------------------

def generate_secure_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def generate_verification_token() -> str:
    return generate_secure_token(32)


def generate_password_reset_token() -> str:
    return generate_secure_token(32)


def generate_device_id(user_agent: Optional[str] = None, ip: Optional[str] = None) -> str:
    """Generate a stable device fingerprint, or a random id when unknown."""
    if not user_agent or not ip:
        return secrets.token_hex(16)
    device_string = f"{user_agent}:{ip}"
    return hashlib.sha256(device_string.encode()).hexdigest()[:32]


def generate_csrf_token(session_token: str) -> str:
    """Derive a CSRF token bound to a session token."""
    return hmac.new(
        settings.csrf_secret.encode(),
        session_token.encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_csrf_token(token: str, session_token: str) -> bool:
    """Constant-time verification of a CSRF token."""
    if not token or not session_token:
        return False
    expected = generate_csrf_token(session_token)
    return hmac.compare_digest(token, expected)


def get_rate_limit_key(identifier: str, action: str) -> str:
    """Rate-limit key namespace.

    Namespaced under ``granada:v1:`` so that rate-limit counters cannot collide
    with the agentic event bus (see docs/EVENT_CATALOG.md).
    """
    return f"granada:v1:ratelimit:{action}:{identifier}"


def hash_sensitive_data(data: str, salt: Optional[str] = None) -> str:
    if not salt:
        salt = secrets.token_hex(16)
    return hashlib.pbkdf2_hmac(
        "sha256", data.encode("utf-8"), salt.encode("utf-8"), 100000
    ).hex()


def decode_token(token: str, expected_type: str = "access") -> Dict[str, Any]:
    """Decode a token and assert its type."""
    payload = decode_access_token(token)
    if payload.get("type") != expected_type:
        raise ValueError(
            f"Expected token type '{expected_type}', got '{payload.get('type')}'"
        )
    return payload