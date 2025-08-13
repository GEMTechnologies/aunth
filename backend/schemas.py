
from pydantic import BaseModel, EmailStr, Field, ConfigDict
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

class UserStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"

# Request schemas
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    full_name: Optional[str] = Field(None, max_length=255)
    locale: str = Field("en", max_length=10)

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str

class PasswordResetRequest(BaseModel):
    email: EmailStr

class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)

class EmailVerifyRequest(BaseModel):
    token: str

class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = Field(None, max_length=255)
    locale: Optional[str] = Field(None, max_length=10)

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8)

# Response schemas
class EmailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    email: str
    is_verified: bool
    is_primary: bool
    created_at: datetime

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    display_name: Optional[str]
    avatar_url: Optional[str]
    locale: str
    created_at: datetime
    status: UserStatus
    primary_email: Optional[EmailResponse] = None

class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    device_id: str
    user_agent: Optional[str]
    ip_last: str
    created_at: datetime
    last_seen_at: datetime

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: UserResponse

class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    display_name: Optional[str]
    avatar_url: Optional[str]
    locale: str
    created_at: datetime
    status: UserStatus
    emails: List[EmailResponse]
    sessions: List[SessionResponse]
    # Add org memberships later

class OrganisationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    name: str
    slug: str
    created_at: datetime

class RoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    key: str
    name: str
    is_system: bool

class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    event: str
    ip: str
    user_agent: Optional[str]
    payload_json: Optional[Dict[str, Any]]
    created_at: datetime

class ErrorResponse(BaseModel):
    detail: str
    error_code: Optional[str] = None
