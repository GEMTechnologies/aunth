from fastapi import APIRouter, Depends, HTTPException, status, Header
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from typing import Optional
from jose import JWTError
from .database import get_db
from . import schemas, service, models
from .security import decode_token
from .config import settings

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

def get_current_user(db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)) -> models.User:
    try:
        data = decode_token(token, expected_scope="access")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(models.User).get(data.get("sub"))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

@router.post("/auth/register", response_model=schemas.UserRead, tags=["Authentication"])
def register(payload: schemas.UserCreate, db: Session = Depends(get_db)):
    user = service.register_user(db, payload)
    return schemas.UserRead(id=user.id, email=user.email, full_name=user.full_name, is_verified=user.is_verified)

@router.post("/auth/login", response_model=schemas.TokenPair, tags=["Authentication"])
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = service.authenticate_user(db, payload.email, payload.password)
    return service.issue_tokens(user, db)

@router.post("/auth/refresh", response_model=schemas.TokenPair, tags=["Authentication"])
def refresh(authorization: Optional[str] = Header(default=None), db: Session = Depends(get_db)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing refresh token")
    refresh_token = authorization.split(" ", 1)[1]
    return service.rotate_refresh(db, refresh_token)

@router.post("/auth/logout", status_code=204, tags=["Authentication"])
def logout(authorization: Optional[str] = Header(default=None), db: Session = Depends(get_db)):
    if authorization and authorization.lower().startswith("bearer "):
        rt = authorization.split(" ", 1)[1]
        service.revoke_refresh(db, rt)
    return

@router.get("/users/me", response_model=schemas.UserRead, tags=["Users"])
def me(current: models.User = Depends(get_current_user)):
    return schemas.UserRead(id=current.id, email=current.email, full_name=current.full_name, is_verified=current.is_verified)

@router.post("/orgs", response_model=schemas.OrgRead, tags=["Organizations"])
def create_org(payload: schemas.OrgCreate, db: Session = Depends(get_db), current: models.User = Depends(get_current_user)):
    org = service.create_org(db, payload.name)
    service.add_member(db, current.id, org.id, role="owner")
    return schemas.OrgRead(id=org.id, name=org.name)
