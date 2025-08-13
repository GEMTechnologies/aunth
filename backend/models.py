
from sqlalchemy import String, DateTime, Boolean, Text, ForeignKey, Integer, JSON, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from datetime import datetime, timezone
import uuid
from typing import Optional, List

def uuid4_str() -> str:
    return str(uuid.uuid4())

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    primary_email_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("emails.id"))
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500))
    locale: Mapped[str] = mapped_column(String(10), default="en")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, suspended, deleted
    
    # Relationships
    emails: Mapped[List["Email"]] = relationship("Email", back_populates="user", foreign_keys="Email.user_id")
    primary_email: Mapped[Optional["Email"]] = relationship("Email", foreign_keys=[primary_email_id], post_update=True)
    password_credential: Mapped[Optional["PasswordCredential"]] = relationship("PasswordCredential", back_populates="user")
    sessions: Mapped[List["Session"]] = relationship("Session", back_populates="user")
    org_memberships: Mapped[List["OrgMember"]] = relationship("OrgMember", back_populates="user")

class Email(Base):
    __tablename__ = "emails"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="emails", foreign_keys=[user_id])

class PasswordCredential(Base):
    __tablename__ = "password_credentials"
    
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    password_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="password_credential")

class Session(Base):
    __tablename__ = "sessions"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str] = mapped_column(String(32), index=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    ip_first: Mapped[str] = mapped_column(String(45))
    ip_last: Mapped[str] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="sessions")
    refresh_tokens: Mapped[List["RefreshToken"]] = relationship("RefreshToken", back_populates="session")

class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    rotated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reuse_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="refresh_tokens")

class Organisation(Base):
    __tablename__ = "organisations"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    members: Mapped[List["OrgMember"]] = relationship("OrgMember", back_populates="organisation")

class Role(Base):
    __tablename__ = "roles"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organisations.id"))
    key: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Relationships
    permissions: Mapped[List["RolePermission"]] = relationship("RolePermission", back_populates="role")

class Permission(Base):
    __tablename__ = "permissions"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    key: Mapped[str] = mapped_column(String(50), unique=True)
    description: Mapped[str] = mapped_column(String(255))

class RolePermission(Base):
    __tablename__ = "role_permissions"
    
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[str] = mapped_column(ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True)
    
    # Relationships
    role: Mapped["Role"] = relationship("Role", back_populates="permissions")

class OrgMember(Base):
    __tablename__ = "org_members"
    
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    invited_by: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    
    # Relationships
    organisation: Mapped["Organisation"] = relationship("Organisation", back_populates="members")
    user: Mapped["User"] = relationship("User", back_populates="org_memberships")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), index=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    event: Mapped[str] = mapped_column(String(50), index=True)
    ip: Mapped[str] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    payload_json: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

class PasswordReset(Base):
    __tablename__ = "password_resets"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

# Add indexes for performance
Index("ix_sessions_user_device", Session.user_id, Session.device_id)
Index("ix_refresh_tokens_expires", RefreshToken.expires_at)
Index("ix_audit_logs_user_event", AuditLog.user_id, AuditLog.event)
