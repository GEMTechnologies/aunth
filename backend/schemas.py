from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = ""

    @field_validator("password")
    @classmethod
    def strong(cls, v: str):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

class UserRead(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    is_verified: bool

class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class EmailRequest(BaseModel):
    email: EmailStr

class PasswordReset(BaseModel):
    token: str
    new_password: str

class OrgCreate(BaseModel):
    name: str

class OrgRead(BaseModel):
    id: str
    name: str
