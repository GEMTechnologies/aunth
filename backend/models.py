from sqlalchemy import String, DateTime, Boolean, Text, ForeignKey, Integer, JSON, Index, UniqueConstraint, Float
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
    last_active_context: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Store user's last active context
    registration_intent: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # student, ngo, business, etc.

    # Relationships
    emails: Mapped[List["Email"]] = relationship("Email", back_populates="user", foreign_keys="Email.user_id")
    primary_email: Mapped[Optional["Email"]] = relationship("Email", foreign_keys=[primary_email_id], post_update=True)
    password_credential: Mapped[Optional["PasswordCredential"]] = relationship("PasswordCredential", back_populates="user")
    password_resets: Mapped[List["PasswordReset"]] = relationship("PasswordReset", back_populates="user", cascade="all, delete-orphan")
    sessions: Mapped[List["Session"]] = relationship("Session", back_populates="user")
    # users -> org_members has one path, but Organisation.owner_user_id is
    # also a FK to users. Without an explicit foreign_keys SQLAlchemy raises
    # AmbiguousForeignKeysError the first time this relationship is used.
    org_memberships: Mapped[List["OrgMember"]] = relationship(
        "OrgMember",
        back_populates="user",
        foreign_keys="OrgMember.user_id",
    )
    owned_orgs: Mapped[List["Organisation"]] = relationship("Organisation", back_populates="owner")
    audit_logs: Mapped[List["AuditLog"]] = relationship("AuditLog", foreign_keys="AuditLog.user_id", back_populates="user")
    oauth_accounts: Mapped[List["OAuthAccount"]] = relationship("OAuthAccount", back_populates="user")

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
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships
    owner = relationship("User", back_populates="owned_orgs")
    # organisations -> org_members is unambiguous, but AuditLog and
    # UserContext both also point at organisations; naming the foreign key
    # keeps this stable if those tables gain further paths.
    members = relationship(
        "OrgMember",
        back_populates="organisation",
        foreign_keys="OrgMember.org_id",
        cascade="all, delete-orphan",
    )
    contexts: Mapped[List["UserContext"]] = relationship("UserContext", back_populates="organisation", cascade="all, delete-orphan", foreign_keys="UserContext.org_id")
    audit_logs = relationship("AuditLog", back_populates="organisation", foreign_keys="AuditLog.org_id")
    saml_providers = relationship("SAMLProvider", back_populates="organisation")

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
    # org_members carries two FKs to users (user_id and invited_by) and two to
    # organisations/roles, so every relationship on this class must name its
    # foreign key explicitly.
    organisation: Mapped["Organisation"] = relationship(
        "Organisation",
        back_populates="members",
        foreign_keys=[org_id],
    )
    user: Mapped["User"] = relationship(
        "User",
        back_populates="org_memberships",
        foreign_keys=[user_id],
    )
    role: Mapped["Role"] = relationship("Role", foreign_keys=[role_id])
    inviter: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[invited_by], viewonly=True
    )

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), index=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    org_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organisations.id"))
    event: Mapped[str] = mapped_column(String(50), index=True)
    # NOT NULL in the schema, yet every one of the ten construction sites
    # omitted it, so each INSERT raised IntegrityError and the surrounding
    # transaction rolled back. The default guarantees the audit row survives
    # even when the caller cannot supply an address; AuditService.record()
    # should be preferred so the real client IP is captured.
    ip: Mapped[str] = mapped_column(String(45), default="unknown")
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    payload_json: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id], back_populates="audit_logs")
    actor: Mapped["User"] = relationship("User", foreign_keys=[actor_user_id])
    organisation: Mapped["Organisation"] = relationship("Organisation", back_populates="audit_logs")

class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships
    user: Mapped["User"] = relationship(back_populates="password_resets")

class OAuthAccount(Base):
    """OAuth account connections for social login"""
    __tablename__ = "oauth_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)  # google, github, facebook, etc.
    provider_user_id: Mapped[str] = mapped_column(String, nullable=False)
    access_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    refresh_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Store additional provider data
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="oauth_accounts")

    __table_args__ = (
        UniqueConstraint('provider', 'provider_user_id', name='unique_provider_user'),
    )

class OAuthState(Base):
    """OAuth state tokens for CSRF protection"""
    __tablename__ = "oauth_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class SAMLProvider(Base):
    """SAML identity providers for SSO"""
    __tablename__ = "saml_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    sso_url: Mapped[str] = mapped_column(String, nullable=False)
    x509_cert: Mapped[str] = mapped_column(String, nullable=False)
    attribute_mapping: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Map SAML attributes to user fields
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships
    organisation: Mapped["Organisation"] = relationship("Organisation", back_populates="saml_providers")

class SAMLAssertion(Base):
    """SAML assertions for audit and debugging"""
    __tablename__ = "saml_assertions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("saml_providers.id"), nullable=False)
    assertion_id: Mapped[str] = mapped_column(String, nullable=False)
    name_id: Mapped[str] = mapped_column(String, nullable=False)
    session_index: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    attributes: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships
    user: Mapped["User"] = relationship("User")
    provider: Mapped["SAMLProvider"] = relationship("SAMLProvider")

class UserContext(Base):
    """Track available contexts/workspaces for users"""
    __tablename__ = "user_contexts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    context_type: Mapped[str] = mapped_column(String(50), nullable=False)  # student, org
    org_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organisations.id"), nullable=True)
    product: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # ngos, business, jobs
    role: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships
    user: Mapped["User"] = relationship("User")
    organisation: Mapped[Optional["Organisation"]] = relationship("Organisation")

class OAuthAuthCode(Base):
    """One-time code handed to the browser after an OAuth redirect.

    The callback used to place the access and refresh tokens directly in the
    redirect URL query string. URLs leak: they land in browser history, in the
    Referer header of any third-party request, in browser extensions, and in
    every access log between the provider and here. The redirect now carries
    only this short-lived, single-use code; the frontend exchanges it over
    POST for the tokens themselves.

    Only the hash of the code is stored, so a database disclosure does not
    yield usable credentials.
    """
    __tablename__ = "oauth_auth_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[Optional[str]] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True)
    redirect_to: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships
    user: Mapped["User"] = relationship("User")


# ---------------------------------------------------------------------------
# Agent runtime ledger
#
# Redis Streams is the delivery mechanism, never the record of what happened.
# Every job, attempt and published event is also written here, inside the same
# transaction as the state change that caused it. A trimmed or lost stream entry
# then costs at worst a redelivery, never a fact. See ADR-0007.
# ---------------------------------------------------------------------------


class Job(Base):
    """One unit of durable agent work.

    ``state`` is deliberately a closed vocabulary rather than a free string:
    the worker's recovery logic branches on it, and a typo in a status column
    would otherwise silently strand a job forever.

    ``idempotency_key`` is what makes redelivery safe. A worker must refuse work
    it has already recorded under the same (org, type, key). NULL keys never
    collide in either PostgreSQL or SQLite, so jobs that are legitimately
    repeatable simply leave the column NULL.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "job_type", "idempotency_key", name="uq_jobs_idempotency"
        ),
    )

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
    CANCELLED = "CANCELLED"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organisations.id"), index=True)
    stream: Mapped[str] = mapped_column(String(128), index=True)
    job_type: Mapped[str] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255))
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default=QUEUED, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    # Retry backoff: a job is not eligible for dispatch before this instant.
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Worker lease. A worker that dies holding a job leaves these set; the
    # recovery sweep reclaims anything whose lease has expired.
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128))
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    failure_category: Mapped[Optional[str]] = mapped_column(String(40))
    #: Which logical agent this work belongs to. This is the column that makes
    #: "one agent per organisation, one shared worker pool" work: a worker loads
    #: the agent named here rather than being dedicated to it. Nullable because
    #: system-level work (the outbox relay, an uncorrelated webhook) belongs to no
    #: agent, and inventing one would attribute work to a customer that did not
    #: ask for it.
    agent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("granada_agents.id"), index=True)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[List["JobAttempt"]] = relationship(
        "JobAttempt", back_populates="job", cascade="all, delete-orphan"
    )


class JobAttempt(Base):
    """One execution of a job.

    Kept as its own rows rather than a column on ``jobs`` so that a DLQ
    investigation can see the whole history - what failed, in which category,
    how long each try took - instead of only the last error.
    """

    __tablename__ = "job_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[Optional[str]] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[Optional[str]] = mapped_column(String(24))
    failure_category: Mapped[Optional[str]] = mapped_column(String(40))
    error: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)

    job: Mapped["Job"] = relationship("Job", back_populates="attempts")


class OutboxEvent(Base):
    """Transactional outbox: an event written in the same commit as the change.

    Publishing straight to Redis from a request handler loses the event whenever
    the process dies between the database commit and the ``XADD`` - and it
    cannot publish an event for a change that later rolls back. Writing here
    first makes "the state changed" and "the event exists" a single atomic
    fact; the relay then moves it to Redis asynchronously.
    """

    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organisations.id"), index=True)
    stream: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)


class InboxEvent(Base):
    """Dedupe ledger for inbound provider webhooks.

    Providers redeliver. Gmail in particular retries a webhook for up to days
    and may deliver the same message id twice. Without a unique constraint on
    the provider's own event id, one inbound mail can create two application
    threads, or an award notification can be handed to two workers at once.

    ``org_id`` is resolved after receipt, because the tenant is not known until
    the mailbox or submission is correlated - which is precisely why this table
    is pre-tenant and why it is not FORCE-protected. See ADR-0007.
    """

    __tablename__ = "inbox_events"
    __table_args__ = (
        UniqueConstraint("source", "external_event_id", name="uq_inbox_source_event"),
    )

    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organisations.id"), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_event_id: Mapped[str] = mapped_column(String(255))
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default=RECEIVED, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    note: Mapped[Optional[str]] = mapped_column(Text)


class ModelInvocation(Base):
    """One call to a language model, recorded whether it succeeded or not.

    Three obligations meet here.

    **Cost control.** A platform that can spend money autonomously without
    recording what it spent cannot be trusted with autonomy. Cost is stored in
    integer micro-dollars because money in binary floating point accumulates
    error, and a budget guard that drifts is worse than no guard.

    **Why did it decide that?** The brief requires every automated decision to
    have an evidence view. That view needs the provider, model and version, the
    prompt version, and the outcome - so those are recorded as first-class
    columns rather than buried in a JSON blob.

    **Data minimisation.** The prompt and response are stored as SHA-256
    digests, not text. The audit question is "was this the same input, and what
    did it produce", which a digest answers; keeping donor and beneficiary text
    in an ops table answers a question nobody should be asking.
    ``settings.model_store_prompts`` can override that for debugging, and doing
    so is a deliberate decision with a privacy cost.

    ``org_id`` is nullable because system-level work (classifying an
    un-attributed inbound webhook) legitimately has no tenant yet.
    """

    __tablename__ = "model_invocations"

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

    CLASSIFICATION = "CLASSIFICATION"
    SYNTHESIS = "SYNTHESIS"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organisations.id"), index=True)
    job_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)

    # What was asked of whom.
    provider: Mapped[str] = mapped_column(String(50), index=True)
    model: Mapped[str] = mapped_column(String(120), index=True)
    model_version: Mapped[Optional[str]] = mapped_column(String(120))
    tier: Mapped[str] = mapped_column(String(20), index=True)
    prompt_version: Mapped[str] = mapped_column(String(50))
    prompt_digest: Mapped[str] = mapped_column(String(64))
    response_digest: Mapped[Optional[str]] = mapped_column(String(64))
    # Only populated when settings.model_store_prompts is on.
    prompt_text: Mapped[Optional[str]] = mapped_column(Text)
    response_text: Mapped[Optional[str]] = mapped_column(Text)

    # What it cost, and how long it took.
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, index=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(20), default=SUCCEEDED, index=True)
    error_category: Mapped[Optional[str]] = mapped_column(String(40))
    error_detail: Mapped[Optional[str]] = mapped_column(Text)

    trace_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


# ---------------------------------------------------------------------------
# Organisation intelligence (Phase 3)
# ---------------------------------------------------------------------------
class OrgFact(Base):
    """One version of one fact about an organisation.

    This table is the Digital Twin. Its single most important property is that
    **every fact carries how it came to be known**, because the security gate
    forbids one specific failure: an AI-inferred value silently becoming a fact
    in a submitted application.

    How that is prevented structurally rather than by convention:

    * ``state`` is one of a closed set, and ``AI_INFERRED`` is in it explicitly
      rather than being represented by a missing value. A fact whose provenance
      is unknown cannot be stored here at all.
    * A version is never overwritten. Rows are appended and superseded, so the
      history of what the platform believed - and on what basis - survives.
    * ``source`` is required. There is no code path that writes a fact without
      saying where it came from, so "nobody knows where this number came from"
      is not representable.

    ``version`` with ``is_current`` rather than deleting: an application that
    was submitted against version 3 must still be explainable after version 4
    arrives, which is the whole point of a "Why?" evidence view.

    ``valid_until`` exists because organisation facts genuinely expire - a
    certificate of registration, a tax exemption, an audit. An expired fact that
    still reads as current is how stale information reaches a funder.
    """

    __tablename__ = "org_facts"

    # Closed set. Anything not here cannot be stored, which is what makes
    # "provenance is known" a property of the schema rather than a hope.
    VERIFIED = "VERIFIED"            # a human with authority confirmed it
    USER_PROVIDED = "USER_PROVIDED"  # the organisation told us
    IMPORTED = "IMPORTED"            # a trusted external source
    AI_INFERRED = "AI_INFERRED"      # a model guessed it - NEVER submission-safe
    EXPIRED = "EXPIRED"              # was true, is no longer trusted

    __table_args__ = (
        UniqueConstraint("org_id", "key", "version", name="uq_org_facts_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    key: Mapped[str] = mapped_column(String(120), index=True)  # e.g. "registration_number"
    value: Mapped[Optional[dict]] = mapped_column(JSON)
    value_type: Mapped[str] = mapped_column(String(20), default="text")

    state: Mapped[str] = mapped_column(String(20), index=True)
    # Only meaningful for AI_INFERRED, and deliberately nullable elsewhere: a
    # confidence attached to a human-verified fact would be meaningless.
    confidence: Mapped[Optional[float]] = mapped_column(Float)

    # Provenance. Required, never null - see the class docstring.
    source: Mapped[str] = mapped_column(String(255))
    source_ref: Mapped[Optional[str]] = mapped_column(String(255))
    evidence_document_id: Mapped[Optional[str]] = mapped_column(String(36))

    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    supersedes_id: Mapped[Optional[str]] = mapped_column(String(36))

    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)

    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[Optional[str]] = mapped_column(String(36))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class Document(Base):
    """One version of one file in the document vault.

    The vault exists because submissions need documents that are current,
    approved, and provably the file that was sent. Four properties carry that:

    ``checksum_sha256``
        Proves the bytes. A submission receipt that names a document id is only
        meaningful if the document cannot change underneath it.

    ``valid_until``
        Certificates, tax clearances and audits expire. A vault that cannot
        express expiry cannot tell you an application is about to be submitted
        with a lapsed certificate.

    ``approval_status``
        A document is not usable merely because it was uploaded. Uploading is
        not approving, and the gap between those two is where wrong documents
        get attached to real applications.

    ``scope``
        Organisation-wide, project-specific or grant-specific. Attaching a
        project's audit to a different project's application is a real error and
        the schema should make it expressible rather than inferable.

    Versioning matches ``OrgFact``: versions are appended, and ``is_current``
    identifies the live one, so an already-submitted application still resolves
    the exact file it used.
    """

    __tablename__ = "documents"

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

    SCOPE_ORGANISATION = "ORGANISATION"
    SCOPE_PROJECT = "PROJECT"
    SCOPE_GRANT = "GRANT"

    __table_args__ = (
        UniqueConstraint("org_id", "storage_key", "version", name="uq_documents_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)

    title: Mapped[str] = mapped_column(String(255))
    doc_type: Mapped[str] = mapped_column(String(80), index=True)
    scope: Mapped[str] = mapped_column(String(20), default=SCOPE_ORGANISATION, index=True)
    scope_ref: Mapped[Optional[str]] = mapped_column(String(36), index=True)

    storage_key: Mapped[str] = mapped_column(String(500))
    checksum_sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    supersedes_id: Mapped[Optional[str]] = mapped_column(String(36))

    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)

    approval_status: Mapped[str] = mapped_column(String(20), default=PENDING, index=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(36))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    uploaded_by: Mapped[Optional[str]] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


# ---------------------------------------------------------------------------
# Opportunity catalogue (Phase 4)
# ---------------------------------------------------------------------------
class Opportunity(Base):
    """The canonical funding opportunity - successor to ``donor_opportunities``.

    Compatibility is the whole point of this table, so it is stated precisely.

    **Every column of the legacy ``donor_opportunities`` is preserved**, with
    the same names and compatible types, because the legacy bot subsystem is the
    only surviving artifact of the existing ingestion pipeline and the producer
    contract is defined in terms of it. See ``docs/BOT_INGESTION_CONTRACT.md``.

    **Both legacy UNIQUE constraints are preserved and must never be dropped:**

    * ``content_hash`` - the contract's content-dedupe key. It is a
      ``varchar(64)``: the width of a SHA-256 hex digest. **The normalisation
      that produces it is not recoverable from anywhere**, because the legacy
      table is empty and the producer code does not exist on this machine. It is
      therefore treated as **opaque**: stored exactly as the producer supplies
      it, never recomputed, never inferred. An adapter that "helpfully"
      recomputed it would silently create duplicates of every opportunity whose
      producer normalised differently.
    * ``source_url`` - the same opportunity re-listed at the same URL is an
      update, not an insert.

    **Three additions**, each addressing something the legacy table cannot
    express:

    ``dedupe_fingerprint``
        An *internal* identity that does not depend on the producer's opaque
        hash, so the agentic pipeline can dedupe opportunities arriving from a
        second producer or a future contract version without guessing how the
        first one hashed. Distinct from ``content_hash`` on purpose: conflating
        them would mean changing one silently changed the other.

    ``org_id``
        Deliberately **NULL and unused**. The funding catalogue is shared, not
        tenant-owned - a funding opportunity published on a website belongs to
        nobody - and the legacy table had no tenant either. Inventing one here
        would mean every tenant re-scraped the world. Tenant scoping begins at
        ``matches``, which is genuinely per-organisation. Recorded as ADR-0009
        because it is a deliberate exception to "everything is tenant-scoped".

    ``contract_version``
        Which producer contract produced this row. The contract explicitly says
        that changing the meaning of ``content_hash`` is a breaking change
        requiring a new version, and that is only enforceable if the version is
        recorded per row.
    """

    __tablename__ = "opportunities"

    # --- legacy donor_opportunities columns, preserved ---
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(Text)
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    amount_min: Mapped[Optional[int]] = mapped_column(Integer)
    amount_max: Mapped[Optional[int]] = mapped_column(Integer)
    currency: Mapped[Optional[str]] = mapped_column(String(10))
    source_url: Mapped[str] = mapped_column(Text, unique=True, index=True)
    source_name: Mapped[str] = mapped_column(String(200), index=True)
    country: Mapped[str] = mapped_column(String(100), index=True)
    sector: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    eligibility_criteria: Mapped[Optional[str]] = mapped_column(Text)
    application_process: Mapped[Optional[str]] = mapped_column(Text)
    contact_email: Mapped[Optional[str]] = mapped_column(String(200))
    contact_phone: Mapped[Optional[str]] = mapped_column(String(50))
    keywords: Mapped[Optional[dict]] = mapped_column(JSON)
    focus_areas: Mapped[Optional[dict]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    last_verified: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    verification_score: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # --- agentic additions ---
    dedupe_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    contract_version: Mapped[str] = mapped_column(String(20), default="v1", index=True)


class OpportunityPayload(Base):
    """The raw payload exactly as the producer delivered it.

    The directive is **"never lose the original payload"**, and the legacy
    ``donor_opportunities`` table has nowhere to put one. This is that place.

    Why a separate table rather than a column: a bot's raw output is a full
    scraped document - HTML, JSON-LD, headers - and it arrives on *every*
    delivery, not only when something changed. Storing it inline would multiply
    the catalogue's size by the crawl frequency and make the query-shaped
    indexes on ``opportunities`` progressively useless.

    Every delivery is recorded, including one that resulted in ``UNCHANGED``.
    That is deliberate: the question this table answers is "what did the producer
    actually say, and when", and a producer that suddenly starts omitting a
    field is a defect you can only see by keeping the deliveries that changed
    nothing.
    """

    __tablename__ = "opportunity_payloads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    opportunity_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("opportunities.id"), index=True
    )
    source_url: Mapped[str] = mapped_column(Text, index=True)
    source_name: Mapped[str] = mapped_column(String(200))
    source_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)

    payload: Mapped[dict] = mapped_column(JSON)  # never null, never truncated
    payload_digest: Mapped[str] = mapped_column(String(64), index=True)
    # The producer's own hash, opaque, stored as delivered.
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    contract_version: Mapped[str] = mapped_column(String(20), default="v1")

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class OpportunityChange(Base):
    """A detected change to an already-known opportunity.

    The brief requires "change detection alerting", and an alert is only useful
    if it says what changed. A deadline moving is materially different from a
    description being reworded: the first can invalidate an application plan,
    the second is noise. So ``material`` is recorded rather than left to the
    reader to infer from the field name.
    """

    __tablename__ = "opportunity_changes"

    # Material changes can invalidate work in progress. An application prepared
    # against a deadline that has since moved is worse than no application.
    MATERIAL_FIELDS = frozenset(
        {"deadline", "amount_min", "amount_max", "eligibility_criteria", "is_active"}
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), index=True)
    field: Mapped[str] = mapped_column(String(80), index=True)
    old_value: Mapped[Optional[str]] = mapped_column(Text)
    new_value: Mapped[Optional[str]] = mapped_column(Text)
    material: Mapped[bool] = mapped_column(Boolean, default=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class IngestionJob(Base):
    """One ingestion run - successor to ``source_ingestion_jobs``.

    Preserves the legacy outcome vocabulary, which already distinguishes
    ``opportunities_found`` ("seen") from ``opportunities_saved`` ("stored").
    That distinction is the whole reason the table is useful: a source that is
    reachable and yields 200 opportunities the pipeline rejects looks identical
    to a dead source unless the two numbers are recorded separately.
    """

    __tablename__ = "ingestion_jobs"

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    source_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    source_name: Mapped[str] = mapped_column(String(200), index=True)
    country: Mapped[Optional[str]] = mapped_column(String(100))
    query: Mapped[Optional[str]] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), default=RUNNING, index=True)
    opportunities_found: Mapped[int] = mapped_column(Integer, default=0)
    opportunities_saved: Mapped[int] = mapped_column(Integer, default=0)
    opportunities_updated: Mapped[int] = mapped_column(Integer, default=0)
    opportunities_rejected: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    contract_version: Mapped[str] = mapped_column(String(20), default="v1")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


# ---------------------------------------------------------------------------
# Matching (Phase 5)
# ---------------------------------------------------------------------------
class OpportunityMatch(Base):
    """The result of qualifying and ranking one opportunity for one organisation.

    This is tenant-owned, unlike the catalogue it points at. ``org_id`` is
    FORCE-protected: who is pursuing which funding is among the most
    commercially sensitive things in the product.

    **Reasons are stored, not just a score.** The brief is explicit that the
    system must store *why*, and a bare percentage cannot answer the two
    questions that matter: "why was this rejected" (must name the gate) and "why
    is this ranked first" (must name the evidence). ``failed_gates`` and
    ``reasons`` are therefore first-class JSON columns.

    ``REJECTED_BY_RULE`` rows are **kept**, not discarded. An organisation that
    cannot see what it was ruled out of, and on what basis, cannot correct its own
    profile - and a rule that never shows its work is a rule nobody trusts.
    """

    __tablename__ = "opportunity_matches"

    MATCHED = "MATCHED"                    # passed every hard gate
    REJECTED_BY_RULE = "REJECTED_BY_RULE"  # failed at least one hard gate
    NEEDS_DATA = "NEEDS_DATA"              # a gate could not be evaluated
    SUPERSEDED = "SUPERSEDED"              # recomputed since

    __table_args__ = (
        UniqueConstraint("org_id", "opportunity_id", name="uq_match_org_opportunity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), index=True)

    state: Mapped[str] = mapped_column(String(20), index=True)
    hard_gate_passed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    #: Plain JSON *lists* of gate names, because the eligibility rules are
    #: deterministic and must be re-derivable without parsing prose.
    failed_gates: Mapped[Optional[dict]] = mapped_column(JSON)
    unknown_gates: Mapped[Optional[dict]] = mapped_column(JSON)
    #: The full evidence trail: every gate, its outcome, and why.
    reasons: Mapped[Optional[dict]] = mapped_column(JSON)

    #: Only ever set for a match that PASSED the hard gates. ``None`` for a
    #: rejected opportunity, because a semantic score for something ineligible is
    #: precisely the number that must not exist.
    semantic_score: Mapped[Optional[float]] = mapped_column(Float, index=True)
    final_score: Mapped[Optional[float]] = mapped_column(Float, index=True)
    rank: Mapped[Optional[int]] = mapped_column(Integer)

    scorer: Mapped[Optional[str]] = mapped_column(String(120))
    prompt_version: Mapped[Optional[str]] = mapped_column(String(50))
    contract_version: Mapped[str] = mapped_column(String(20), default="v1")

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


# ---------------------------------------------------------------------------
# Decision gateway (Phase 5b)
# ---------------------------------------------------------------------------
class DecisionRecord(Base):
    """Every decision Granada made, why, and whether it was allowed to matter.

    The brief requires that a decision influencing an external action is
    auditable, with a named entry rather than the sentence "AI decided yes". This
    table is that entry: the question set version, the answers, the confidence
    that was (or was not) reported, the policy verdict, and the correlation id
    tying it to the workflow and the log lines.

    Two columns carry the design's weight:

    ``state_hash``
        A fingerprint of the state the decision was made on. It is what makes a
        cache key correct and what makes a stored decision *invalidatable*: when
        the organisation profile or the opportunity changes, the fingerprint
        changes, and the old decision stops being reusable. Without it a cached
        verdict would outlive the facts it was based on.

    ``shadow`` / ``shadow_of``
        A shadow decision is recorded with ``shadow=True`` and a pointer to the
        decision that actually acted. This is what lets the platform answer "how
        often would Jev have agreed with our rules" from real data, without any
        possibility of the shadow answer having influenced anything - a claim
        that is enforced in code, because the shadow result is never returned as
        the acting one.

    ``state_snapshot`` is nullable and off by default. References and a hash are
    enough for audit, and storing the state would put donor and beneficiary
    content into an operations table.
    """

    __tablename__ = "decision_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    #: Kept as a plain column name matching the brief's vocabulary. Tenant
    #: scoping uses ``organisation_id``, which is what RLS filters on.
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    organisation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("organisations.id"), index=True
    )

    decision_type: Mapped[str] = mapped_column(String(80), index=True)
    subject_type: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    subject_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    workflow_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)

    provider: Mapped[str] = mapped_column(String(50), index=True)
    model: Mapped[Optional[str]] = mapped_column(String(120))
    question_schema_version: Mapped[str] = mapped_column(String(20), default="v1", index=True)
    state_hash: Mapped[str] = mapped_column(String(64), index=True)
    state_snapshot: Mapped[Optional[dict]] = mapped_column(JSON)

    answers: Mapped[Optional[dict]] = mapped_column(JSON)
    confidences: Mapped[Optional[dict]] = mapped_column(JSON)
    #: The lowest reported confidence, or None. Nullable because a provider that
    #: reports none must not be recorded as if it were confident.
    confidence: Mapped[Optional[float]] = mapped_column(Float, index=True)
    probabilities: Mapped[Optional[dict]] = mapped_column(JSON)

    policy_outcome: Mapped[Optional[bool]] = mapped_column(Boolean, index=True)
    policy_detail: Mapped[Optional[dict]] = mapped_column(JSON)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    shadow: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    shadow_of: Mapped[Optional[str]] = mapped_column(String(36), index=True)

    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)


# ---------------------------------------------------------------------------
# Application workspace (Phase 6)
# ---------------------------------------------------------------------------
class Application(Base):
    """One application workspace per organisation and opportunity.

    The brief requires a single workspace carrying the whole lifecycle, and the
    unique constraint on ``(org_id, opportunity_id)`` is what makes "one" true
    rather than aspirational. Two workspaces for one opportunity would mean two
    answers being written to the same funder, which is worse than either of them
    alone.

    ``state`` is a closed set and transitions are validated in code
    (``agent/workspace.py``) rather than by a CHECK constraint, because the
    *reason* a transition is refused has to be reportable and a constraint
    violation is not. Every accepted transition writes an
    ``ApplicationTransition`` row, so the history is append-only and an
    application that reached ``SUBMITTED`` can be explained afterwards.

    ``version`` increments on every accepted transition. The brief requires full
    version history, and this is what a submission receipt can cite: the state of
    the workspace at the moment it was sent, not the state now.
    """

    __tablename__ = "applications"

    __table_args__ = (
        UniqueConstraint("org_id", "opportunity_id", name="uq_application_org_opportunity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), index=True)

    state: Mapped[str] = mapped_column(String(40), index=True)
    #: Incremented on every accepted transition. Cited by a receipt.
    version: Mapped[int] = mapped_column(Integer, default=1)

    created_by: Mapped[Optional[str]] = mapped_column(String(36))
    assigned_to: Mapped[Optional[str]] = mapped_column(String(36), index=True)

    #: Why the workspace is where it is. Not a log line - a queryable reason.
    state_reason: Mapped[Optional[str]] = mapped_column(Text)
    #: Set when a human approved the next step; cleared when it is consumed.
    approved_by: Mapped[Optional[str]] = mapped_column(String(36))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    #: The external reference the funder returned. A submission without one is
    #: not a submission, and the state machine refuses to claim otherwise.
    submission_receipt: Mapped[Optional[str]] = mapped_column(Text)
    submission_adapter: Mapped[Optional[str]] = mapped_column(String(50))
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)

    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[Optional[str]] = mapped_column(String(40), index=True)

    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ApplicationTransition(Base):
    """The append-only history of a workspace.

    Never updated, never deleted. This is what makes "full version history" real,
    and it is the difference between knowing an application was submitted and
    being able to say which version of it, by whom, and on what authority.

    ``actor_type`` distinguishes a human from an agent from the system, because
    "who did this" has a different answer and a different consequence for each.
    """

    __tablename__ = "application_transitions"

    ACTOR_HUMAN = "HUMAN"
    ACTOR_AGENT = "AGENT"
    ACTOR_SYSTEM = "SYSTEM"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)

    from_state: Mapped[Optional[str]] = mapped_column(String(40))
    to_state: Mapped[str] = mapped_column(String(40), index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text)

    actor_type: Mapped[str] = mapped_column(String(20), default=ACTOR_SYSTEM, index=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String(36))
    #: The decision that authorised this transition, when a decision did.
    decision_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    #: The job that performed it, when an agent did.
    job_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)

    version: Mapped[int] = mapped_column(Integer, default=1)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


# ---------------------------------------------------------------------------
# The persistent Granada Agent (Phase 6)
# ---------------------------------------------------------------------------
class GranadaAgent(Base):
    """One persistent logical agent per organisation. The unit of autonomy.

    The product promise is *"create your profile once; Granada creates your
    agent; your agent works for you continuously."* This row **is** that agent.

    **Logical, not a process.** Ten thousand NGOs get ten thousand of these rows
    and **one** shared worker pool. There is deliberately no process, thread,
    scheduler or Redis lock per agent, because that model costs a fixed amount per
    customer whether or not they are doing anything. Instead every job carries
    ``agent_id``; a worker picks up a job, loads *that* agent's state, does the
    work, stores the result, and becomes available for another organisation. The
    customer experiences a private 24/7 agent; the operator runs one fleet.

    **This is the scope for everything.** Facts, documents, applications, mail
    identities, decisions and workflows all belong to an agent, so "which
    organisation's memory am I acting on" has exactly one answer and it is not
    inferred from whichever request happens to be in flight.

    ``autonomy`` lives here rather than on the organisation because the agent is
    what acts. A specialist agent can never exceed its parent's level - the parent
    is the ceiling, and that is enforced in code rather than by convention.
    """

    __tablename__ = "granada_agents"

    #: The four verticals share one engine. Kept explicit so domain data stays
    #: separated without four copies of the runtime.
    VERTICAL_NGO = "NGO"
    VERTICAL_ACADEMIA = "ACADEMIA"
    VERTICAL_BUSINESS = "BUSINESS"
    VERTICAL_JOBS = "JOBS"
    VERTICALS = (VERTICAL_NGO, VERTICAL_ACADEMIA, VERTICAL_BUSINESS, VERTICAL_JOBS)

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    PROVISIONING = "PROVISIONING"
    SUSPENDED = "SUSPENDED"

    __table_args__ = (
        UniqueConstraint("org_id", name="uq_agent_org"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)

    #: What the organisation sees: "Your Granada Agent". Not an internal id.
    display_name: Mapped[str] = mapped_column(String(200))
    vertical: Mapped[str] = mapped_column(String(20), default=VERTICAL_NGO, index=True)
    status: Mapped[str] = mapped_column(String(20), default=PROVISIONING, index=True)

    #: The authority ceiling for every specialist under this agent.
    autonomy: Mapped[str] = mapped_column(String(30), default="MONITOR_ONLY", index=True)
    #: Configuration that is genuinely per-agent rather than per-org: funding
    #: preferences, search cadence, notification settings.
    settings: Mapped[Optional[dict]] = mapped_column(JSON)

    #: Denormalised so "last worked 3 minutes ago" is one read rather than an
    #: aggregate over the ledger. Updated by the worker that does the work.
    last_active_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    #: Bumped on every material change, so a cached or in-flight decision can tell
    #: that the agent it was made for has changed underneath it.
    version: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AgentSpecialist(Base):
    """A named specialist under an agent: Opportunity Hunter, Proposal Writer, ...

    Exists so the organisation can see *which* part of its agent is doing what -
    "Proposal Agent - preparing EU application" - rather than a single opaque
    worker. That visibility is the difference between an agent the customer trusts
    and a black box they have to take on faith.

    ``current_activity`` is a short human string, set by the worker while it holds
    a job and cleared afterwards. It is deliberately not a job log: the durable
    record is ``jobs`` and ``job_attempts``, and this is only what to display.
    """

    __tablename__ = "agent_specialists"

    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"

    __table_args__ = (
        UniqueConstraint("agent_id", "key", name="uq_specialist_agent_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    agent_id: Mapped[str] = mapped_column(ForeignKey("granada_agents.id"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)

    #: One of OPPORTUNITY_HUNTER, MATCHER, DONOR_RESEARCHER, PROPOSAL_WRITER,
    #: BUDGET, COMPLIANCE, DOCUMENT, EMAIL, SUBMISSION, FOLLOW_UP.
    key: Mapped[str] = mapped_column(String(40), index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default=IDLE, index=True)
    current_activity: Mapped[Optional[str]] = mapped_column(String(255))
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: Rolling counters for the agent summary. Reset deliberately, never by a
    #: rollover that could silently zero a customer's visible history.
    runs_completed: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class AgentWorkflow(Base):
    """A durable workflow instance belonging to one agent.

    **Every autonomous workflow belongs to a persistent agent.** This is the
    correction that keeps Granada from becoming ordinary background-job software:
    a workflow is not anonymous work, it is *War Child's agent pursuing this
    opportunity*, and the record says so.

    ``subject_type``/``subject_id`` point at whatever the workflow is about - an
    opportunity, an application, a mail thread - so one table serves all of them
    without a column per case. ``next_run_at`` is the wake-up: a workflow waiting
    on a deadline or a follow-up window is scheduled here rather than being held by
    a sleeping process.
    """

    __tablename__ = "agent_workflows"

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"          # waiting on a person, a document, or a deadline
    BLOCKED = "BLOCKED"          # waiting on something we cannot control
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    SUBJECT_OPPORTUNITY = "OPPORTUNITY"
    SUBJECT_APPLICATION = "APPLICATION"
    SUBJECT_MAIL = "MAIL"
    SUBJECT_ORG = "ORGANISATION"

    __table_args__ = (
        UniqueConstraint(
            "agent_id", "workflow_type", "subject_type", "subject_id",
            name="uq_workflow_agent_subject",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    agent_id: Mapped[str] = mapped_column(ForeignKey("granada_agents.id"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)

    #: Which specialist owns the next step. Null while unassigned.
    specialist_key: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    workflow_type: Mapped[str] = mapped_column(String(60), index=True)
    state: Mapped[str] = mapped_column(String(20), default=PENDING, index=True)

    subject_type: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    subject_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)

    #: The wake-up. A workflow that needs to run in three days is scheduled, not
    #: held open by a process.
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: Why it is waiting, in words a human can act on.
    waiting_on: Mapped[Optional[str]] = mapped_column(String(255))

    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    context: Mapped[Optional[dict]] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# Add indexes for performance
Index("ix_sessions_user_device", Session.user_id, Session.device_id)
Index("ix_jobs_dispatch", Job.state, Job.available_at)
Index("ix_jobs_lease", Job.lease_expires_at)
Index("ix_outbox_unpublished", OutboxEvent.published_at, OutboxEvent.created_at)
Index("ix_model_invocations_org_created", ModelInvocation.org_id, ModelInvocation.created_at)
Index("ix_model_invocations_model_status", ModelInvocation.model, ModelInvocation.status)
# The Digital Twin is read by "give me the current facts for this org", and by
# "which facts are about to expire" - both of which are index-shaped.
Index("ix_org_facts_current", OrgFact.org_id, OrgFact.is_current, OrgFact.key)
Index("ix_documents_current", Document.org_id, Document.is_current, Document.doc_type)
Index("ix_documents_expiry", Document.org_id, Document.valid_until)
# The legacy table's query-shaped indexes, preserved: "active opportunities for
# a country/sector ordered by deadline" is the matching and deadline-scanning
# path the engine needs. Recreating them is not decoration - losing them would
# turn the hot matching query into a sequential scan.
Index("ix_opportunities_active_country_deadline", Opportunity.is_active, Opportunity.country, Opportunity.deadline)
Index("ix_opportunities_active_sector_deadline", Opportunity.is_active, Opportunity.sector, Opportunity.deadline)
Index("ix_opportunities_source_scraped", Opportunity.source_name, Opportunity.scraped_at)
Index("ix_opportunity_changes_material", OpportunityChange.material, OpportunityChange.detected_at)
Index("ix_opportunity_payloads_url_received", OpportunityPayload.source_url, OpportunityPayload.received_at)
Index("ix_matches_org_state_score", OpportunityMatch.org_id, OpportunityMatch.state, OpportunityMatch.final_score)
Index("ix_matches_org_rank", OpportunityMatch.org_id, OpportunityMatch.rank)
Index("ix_decisions_org_type_created", DecisionRecord.organisation_id, DecisionRecord.decision_type, DecisionRecord.created_at)
Index("ix_decisions_cache_lookup", DecisionRecord.organisation_id, DecisionRecord.decision_type, DecisionRecord.state_hash, DecisionRecord.provider)
Index("ix_applications_org_state", Application.org_id, Application.state, Application.deadline)
Index("ix_applications_org_updated", Application.org_id, Application.updated_at)
Index("ix_transitions_application_version", ApplicationTransition.application_id, ApplicationTransition.version)
# The agent layer. The dispatcher query is "what is due for any agent", and the
# per-agent view is "what is my agent doing" - both index-shaped.
Index("ix_jobs_agent_dispatch", Job.agent_id, Job.state, Job.available_at)
Index("ix_workflows_due", AgentWorkflow.state, AgentWorkflow.next_run_at)
Index("ix_workflows_agent_state", AgentWorkflow.agent_id, AgentWorkflow.state)
Index("ix_specialists_agent_status", AgentSpecialist.agent_id, AgentSpecialist.status)
Index("ix_refresh_tokens_expires", RefreshToken.expires_at)
Index("ix_audit_logs_user_event", AuditLog.user_id, AuditLog.event)
Index("ix_oauth_accounts_user", OAuthAccount.user_id)
Index("ix_oauth_states_state", OAuthState.state)
Index("ix_saml_providers_org", SAMLProvider.org_id)
Index("ix_saml_assertions_provider", SAMLAssertion.provider_id)