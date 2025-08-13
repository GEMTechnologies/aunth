from datetime import datetime, timedelta
from typing import Optional
from jose import jwt, JWTError
from passlib.context import CryptContext
from .config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)

def create_token(subject: str, expires_delta: timedelta, scope: str) -> str:
    now = datetime.utcnow()
    payload = {"sub": subject, "exp": now + expires_delta, "iat": now, "scope": scope}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

def decode_token(token: str, expected_scope: Optional[str] = None) -> dict:
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        if expected_scope and data.get("scope") != expected_scope:
            raise JWTError("Invalid token scope")
        return data
    except JWTError as e:
        raise
