from datetime import timedelta, datetime
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException, status
from . import models, schemas
from .security import hash_password, verify_password, create_token
from .config import settings

def register_user(db: Session, payload: schemas.UserCreate) -> models.User:
    user = models.User(email=payload.email, full_name=payload.full_name or "", hashed_password=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Email already registered")
    return user

def authenticate_user(db: Session, email: str, password: str) -> models.User:
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="User is inactive")
    return user

def issue_tokens(user: models.User, db: Session) -> schemas.TokenPair:
    access = create_token(user.id, timedelta(minutes=settings.access_token_expire_minutes), scope="access")
    refresh = create_token(user.id, timedelta(days=settings.refresh_token_expire_days), scope="refresh")
    # store refresh
    store = models.TokenStore(user_id=user.id, refresh_token=refresh, expires_at=datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days))
    db.add(store)
    db.commit()
    return schemas.TokenPair(access_token=access, refresh_token=refresh)

def rotate_refresh(db: Session, refresh_token: str) -> schemas.TokenPair:
    from jose import JWTError
    from .security import decode_token
    try:
        data = decode_token(refresh_token, expected_scope="refresh")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    # locate and rotate
    row = db.query(models.TokenStore).filter(models.TokenStore.refresh_token==refresh_token).first()
    if not row or row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="Refresh token expired or revoked")
    user = db.query(models.User).get(row.user_id)
    # revoke old
    db.delete(row)
    db.commit()
    return issue_tokens(user, db)

def revoke_refresh(db: Session, refresh_token: str):
    row = db.query(models.TokenStore).filter(models.TokenStore.refresh_token==refresh_token).first()
    if row:
        db.delete(row)
        db.commit()

def create_org(db: Session, name: str) -> models.Organization:
    org = models.Organization(name=name)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org

def add_member(db: Session, user_id: str, org_id: str, role: str="member"):
    m = models.Membership(user_id=user_id, organization_id=org_id, role=role)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m
