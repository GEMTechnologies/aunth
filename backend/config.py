"""Granada Authentication Service - configuration.

Phase 1 remediation.

This module previously declared the same field two or three times inside
``Settings``. Pydantic keeps the LAST declaration, so several values silently
meant something other than what the file appeared to say:

  * ``jwt_audience`` was declared as a ``str`` and later as a ``List[str]``;
  * ``jwt_issuer`` had two different defaults;
  * ``refresh_token_ttl_days`` was 7 and then 30;
  * ``argon2_parallelism`` was 1 and then 2;
  * ``frontend_url`` was port 3000 and then 3001;
  * the Google/GitHub OAuth credentials were empty and then placeholders;
  * ``csrf_secret`` had two different defaults.

It also shipped *known* fallback secrets. If the environment was missing a
variable the service would start with ``your-jwt-secret-here`` and sign every
token with a value published in the repository. Secrets are now required in
non-development environments and rejected when they still hold their default.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Values that must never be used to sign a real token.
INSECURE_DEFAULTS = frozenset(
    {
        "",
        "your-secret-key-here",
        "your-jwt-secret-here",
        "csrf-secret-key",
        "csrf-secret-key-change-in-production",
        "change-in-production",
        "changeme",
        # Present in the repository's development .env and in older
        # documentation; long enough to look real, so it must be blocked too.
        "your-very-secure-secret-key-change-in-production-minimum-32-chars",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
        # Pydantic reserves the ``model_`` prefix because a field named
        # ``model_dump`` or ``model_validate`` would shadow its own API. The
        # model-gateway settings below genuinely need that prefix - "model
        # provider" and "model classification model" are the domain's words -
        # and none of them collides with a real ``BaseModel`` attribute.
        #
        # Rather than rely on "none of them collides today", that invariant is
        # asserted in tests/test_model_gateway.py::test_no_setting_shadows_a_pydantic_attribute.
        # A future `model_validate` setting would be caught there instead of
        # silently breaking serialisation.
        protected_namespaces=("settings_",),
    )

    # -- Application ------------------------------------------------------
    app_env: str = "development"
    debug: bool = True
    api_title: str = "Granada Authentication Service"
    api_version: str = "1.0.0"
    log_level: str = "INFO"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # -- Secrets (required outside development) ---------------------------
    secret_key: str = "your-secret-key-here"
    jwt_secret: str = "your-jwt-secret-here"
    csrf_secret: str = "csrf-secret-key-change-in-production"

    # -- Tokens -----------------------------------------------------------
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "granada.auth"
    jwt_audience: List[str] = ["granada-web", "granada-api"]
    access_token_ttl_min: int = 30
    refresh_token_ttl_days: int = 30
    token_rotation: bool = True

    # -- Refresh token delivery -------------------------------------------
    # "body"  -> refresh_token travels in the JSON response. Correct for
    #            first-party API and CLI clients that must hold the credential.
    # "cookie"-> refresh_token is set as an HttpOnly cookie and OMITTED from
    #            the JSON body. Required for browser clients: a token in
    #            localStorage is readable by any script that manages to run on
    #            the origin, so XSS becomes credential theft.
    refresh_token_delivery: str = "body"

    refresh_token_cookie_name: str = "granada_refresh"
    #: Scoped to the auth surface so the credential is not attached to every
    #: request the browser makes to this origin, including static assets.
    refresh_token_cookie_path: str = "/api/v1/auth"
    #: "lax" is correct when the SPA is served from the same site as this API.
    #: "none" is required when they are on different sites, and browsers drop
    #: a SameSite=None cookie unless it is also Secure -- guarded at startup.
    refresh_token_cookie_samesite: str = "lax"
    #: Must be True for SameSite=None, and for any non-localhost deployment.
    refresh_token_cookie_secure: bool = False
    refresh_token_cookie_domain: Optional[str] = None

    # -- Password hashing (Argon2id) --------------------------------------
    argon2_memory: int = 65536  # 64 MB
    argon2_time: int = 3
    argon2_parallelism: int = 2

    # -- Database / cache -------------------------------------------------
    database_url: str = "sqlite:///./test.db"
    database_echo: bool = False

    # -- Database bounds --------------------------------------------------
    #
    # Every one of these has a default that means "no limit", and no limit is not a neutral
    # choice for a service. They are settings rather than constants because the right value
    # depends on the deployment's network, and a bound nobody can tune is a bound somebody
    # will remove.
    #
    # Seconds to wait for a TCP connect. libpq defaults to none, so an unreachable host blocks
    # for the operating system's TCP timeout - minutes - and `pool_pre_ping` puts that on the
    # path of every request that checks out a connection.
    database_connect_timeout: int = 10

    # Milliseconds a single statement may run. A BACKSTOP against a runaway query, not a
    # performance policy: nothing legitimate on the request path takes two minutes, and without
    # it one bad plan holds a pool slot forever.
    database_statement_timeout_ms: int = 120_000

    # Milliseconds a connection may sit idle INSIDE an open transaction. PostgreSQL defaults
    # this to 0 - disabled - so a leaked transaction holds its locks indefinitely while every
    # writer behind it waits.
    database_idle_transaction_timeout_ms: int = 300_000

    # Seconds after which a pooled connection is discarded rather than reused. Below the idle
    # timeout of the NATs and load balancers these deployments sit behind, which otherwise hand
    # back a connection that is already dead.
    database_pool_recycle_seconds: int = 300
    # The connection the /metrics operational gauges are read through.
    #
    # DELIBERATELY SEPARATE from `database_url`, and normally unset. The operational
    # gauges are cross-tenant counts (outbox backlog, stuck jobs, unknown submissions),
    # and the application role is RLS-bound, so it reads ZERO rows unscoped:
    #
    #     app role, unscoped: outbox backlog 0, jobs 0   <- the tables are not empty
    #
    # A gauge computed from that reports 0 on a system whose relay died hours ago, and
    # the alert on it never fires. So the block is either measured through a role that
    # can read across tenants (the owner, or a BYPASSRLS metrics role - the same
    # prerequisite as backups) or it is OMITTED and reported as unavailable. It is never
    # reported as zero.
    metrics_database_url: Optional[str] = None

    # -- The fleet credential (ADR-0011) ------------------------------------
    #
    # THE SAME RLS PROBLEM AS `metrics_database_url` ABOVE, in the one place it actually broke the
    # product. `FleetDispatcher.due_workflows()` reads `agent_workflows` WITHOUT binding a tenant,
    # because it must discover work across the whole fleet. But the table is FORCE ROW LEVEL SECURITY
    # with `org_id = app.current_org()`, and an unbound `app.current_org()` is NULL - so the query
    # returns ZERO ROWS whatever exists.
    #
    # Proven on the first deployment, not inferred:
    #
    #     INSERTED as superuser, total rows = 1
    #     AS granada_app, UNSCOPED (what due_workflows does) = 0
    #
    # The sweep reported `dispatched=0 errors=0` throughout: blind, not idle, and healthy-looking.
    # `granada_agents`, `jobs` and `agent_activity` share the shape, so nothing could be claimed or
    # dispatched either. Matching, qualification, the application workspace, mail, submission and
    # grants were all unreachable.
    #
    # Set this to the `granada_fleet` role (LOGIN BYPASSRLS, created by sql/fleet_role.sql) for the
    # WORKER and the RELAY only. It must NOT be set for the API: giving the request path BYPASSRLS
    # would remove the product's central security property - that a request cannot read another
    # tenant's data - in order to fix a worker problem.
    #
    # Unset means "use the application role", so SQLite and the test suite are unchanged.
    fleet_database_url: Optional[str] = None
    redis_url: str = "redis://0.0.0.0:6379/0"

    # -- URLs -------------------------------------------------------------
    # Comma-separated hosts accepted by TrustedHostMiddleware in production.
    #
    # This was hardcoded in main.py to a placeholder domain, which made every request - including
    # the container healthcheck - return 400 Invalid host header on the first real deployment.
    # A trusted-host allowlist that cannot name the real host protects nothing, so it is
    # configuration. The loopback names are always added by main.py for the healthcheck.
    allowed_hosts: str = ""

    # -- Producer ingestion -------------------------------------------------
    #
    # The key a crawler presents as X-Bot-Key. Empty DISABLES the ingestion endpoint rather than
    # opening it: an endpoint that writes to the shared catalogue every organisation reads must be
    # closed by default, and a deployment that has not thought about it must not be reachable.
    #
    # One key for all producers today. The contract's `search_bots` table implies per-bot identity
    # and revocation, which needs a migration - so this is the honest v1, not the finished shape.
    ingest_bot_key: str = ""

    # -- API documentation --------------------------------------------------
    #
    # The interactive docs (/docs, /redoc) used to be gated on debug alone:
    #
    #     docs_url="/docs" if settings.debug else None
    #
    # which made them all-or-nothing with a flag that ALSO changes error verbosity, CORS and cookie
    # behaviour. Wanting the API reference available is not the same as wanting production to run in
    # debug mode, and tying them together meant the only way to see the docs was to weaken everything
    # else.
    #
    # Default False: the schema describes every endpoint and is not something to publish by accident.
    api_docs_enabled: bool = False

    api_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:3001"
    web_url: str = "http://0.0.0.0:3001"

    # -- OAuth providers --------------------------------------------------
    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    facebook_client_id: str = ""
    facebook_client_secret: str = ""

    # -- SAML / SSO -------------------------------------------------------
    saml_sp_entity_id: str = "granada-auth"
    saml_sp_acs_url: str = ""
    saml_sp_x509_cert: str = ""
    saml_sp_private_key: str = ""

    # -- Email ------------------------------------------------------------
    email_from: str = "noreply@granada.example"
    email_from_name: str = "Granada Auth"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_tls: bool = True
    smtp_ssl: bool = False

    # -- Cookies ----------------------------------------------------------
    cookie_domain: str = ".localhost"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"

    # -- CORS -------------------------------------------------------------
    cors_origins: List[str] = [
        "http://localhost:3000",
        "http://localhost:3001",
    ]
    allowed_origins: List[str] = [
        "http://0.0.0.0:3001",
        "http://localhost:3001",
        "http://0.0.0.0:3000",
        "http://localhost:3000",
    ]

    # -- Rate limiting ----------------------------------------------------
    rate_limit_requests: int = 100
    rate_limit_window: int = 60

    # -- Sessions ---------------------------------------------------------
    session_timeout_hours: int = 24
    max_sessions_per_user: int = 10

    # -- Verification / reset --------------------------------------------
    verification_token_ttl_hours: int = 24
    password_reset_token_ttl_hours: int = 1

    # -- Uploads ----------------------------------------------------------
    max_upload_size: int = 10 * 1024 * 1024
    allowed_avatar_extensions: List[str] = [".jpg", ".jpeg", ".png", ".gif"]

    # -- Model gateway ----------------------------------------------------
    # Provider-neutral by construction: ``model_provider`` selects an adapter,
    # and nothing above the gateway knows which vendor is behind it. Small
    # models are used for classification and strong ones for synthesis, because
    # paying synthesis prices to decide whether an email is an acknowledgement
    # is the most common way an agent platform becomes uneconomic.
    model_provider: str = "null"
    model_classification_model: str = ""
    model_synthesis_model: str = ""
    model_api_key: str = ""
    model_base_url: str = ""
    model_timeout_seconds: int = 60
    # Per-call cost ceiling in micro-dollars (1e-6 USD). Integer, not float:
    # money in binary floating point accumulates error, and a budget guard that
    # drifts is worse than none. Default 5_000_000 = $5.00.
    model_max_cost_micros_per_call: int = 5_000_000
    # Hard ceiling for one organisation in a rolling 24h window.
    model_max_cost_micros_per_day: int = 50_000_000
    # Prompts and responses are digested, not stored, unless this is on.
    model_store_prompts: bool = False

    # -- Decision gateway -------------------------------------------------
    # Provider-neutral bounded decisions. The default configuration needs no
    # API key and no network, because the brief requires that the application
    # boots and runs standalone. `rules` and `local` are both Granada's own.
    decision_gateway_enabled: bool = True
    decision_provider: str = "rules"
    # SHADOW is the only stage that cannot influence an action, and it comes first: record what
    # the engine decided, compare it with Granada's own answer, and enable authority only on
    # measured reliability rather than on a vendor benchmark.
    decision_rollout_stage: str = "SHADOW"
    decision_autonomy: str = "MONITOR_ONLY"
    decision_cache_ttl_seconds: int = 3600
    decision_timeout_seconds: int = 20
    # Off by default. A decision fingerprint plus references is enough to audit
    # with, and the state may contain donor and beneficiary content.
    decision_store_state: bool = False


    # -- Fleet dispatcher (Phase 6d) --------------------------------------
    # One fleet-level loop, NOT one timer per agent. Ten thousand organisations
    # are ten thousand rows in one query, not ten thousand scheduled tasks.
    fleet_dispatch_interval_seconds: int = 15
    #: Bounded per sweep, so the loop cannot load the whole table.
    fleet_dispatch_batch_size: int = 200
    #: Fairness cap: how many workflows one agent may have dispatched in a single
    #: sweep, so a large organisation cannot fill the batch.
    fleet_per_agent_limit: int = 25

    # -- Autonomous mail (Phase 7c) ---------------------------------------
    #: THE KILL SWITCH for unattended outbound mail. Defaults to **off**, so the
    #: capability is inert on every deployment until somebody turns it on
    #: deliberately. An operator can stop every autonomous send in the fleet with this
    #: one setting, which matters at 3am when there is no time to audit which
    #: organisations opted in.
    autonomous_mail_enabled: bool = False

    # -- Validators -------------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        """Return an uppercase level.

        The caller previously did ``getattr(settings.log_level.upper())``,
        which passes one argument to a two-argument builtin and raises
        TypeError during import of ``main``.
        """
        level = (value or "INFO").strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if level not in allowed:
            raise ValueError(
                f"log_level must be one of {sorted(allowed)}, got {value!r}"
            )
        return level

    @field_validator("model_provider")
    @classmethod
    def _known_model_provider(cls, value: str) -> str:
        """Reject an unknown provider at startup rather than at first call.

        A typo here would otherwise surface as a failed agent run at 3am
        rather than as a service that refuses to boot.
        """
        provider = (value or "null").strip().lower()
        allowed = {"null", "scripted", "openai_compatible", "anthropic"}
        if provider not in allowed:
            raise ValueError(
                f"model_provider must be one of {sorted(allowed)}, got {value!r}"
            )
        return provider

    @field_validator("decision_provider")
    @classmethod
    def _known_decision_provider(cls, value: str) -> str:
        """Reject an unknown provider at startup rather than at first decision.

        A typo here would otherwise surface as every decision falling through to
        nothing at 3am.
        """
        provider = (value or "rules").strip().lower()
        allowed = {"rules", "local", "llm", "hybrid"}
        if provider not in allowed:
            raise ValueError(
                f"decision_provider must be one of {sorted(allowed)}, got {value!r}"
            )
        return provider

    @field_validator("decision_rollout_stage")
    @classmethod
    def _known_rollout_stage(cls, value: str) -> str:
        """A typo must not silently become the most permissive stage."""
        stage = (value or "SHADOW").strip().upper()
        allowed = {"SHADOW", "ADVISORY", "INTERNAL_AUTOMATION", "LOW_RISK_EXTERNAL_AUTOMATION"}
        if stage not in allowed:
            raise ValueError(
                f"decision_rollout_stage must be one of {sorted(allowed)}, got {value!r}"
            )
        return stage

    @model_validator(mode="after")
    def _reject_insecure_secrets_outside_dev(self) -> "Settings":
        """Fail closed when a signing secret is still a shipped default."""
        if self.app_env.lower() in {"development", "dev", "test", "local", "ci"}:
            return self

        for field in ("secret_key", "jwt_secret", "csrf_secret"):
            value = getattr(self, field)
            if not value or value in INSECURE_DEFAULTS:
                raise ValueError(
                    f"{field} is still set to a default placeholder. "
                    f"Set a real value in the environment before starting "
                    f"with APP_ENV={self.app_env}."
                )

        if len(self.jwt_secret) < 32:
            raise ValueError("jwt_secret must be at least 32 characters")
        return self

    @field_validator("refresh_token_delivery")
    @classmethod
    def _known_delivery(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        if mode not in {"body", "cookie"}:
            raise ValueError(
                f"REFRESH_TOKEN_DELIVERY must be 'body' or 'cookie', got {value!r}. "
                "'body' is correct for API clients; 'cookie' is required for "
                "browser clients."
            )
        return mode

    @field_validator("refresh_token_cookie_samesite")
    @classmethod
    def _known_samesite(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        if mode not in {"lax", "strict", "none"}:
            raise ValueError(
                f"REFRESH_TOKEN_COOKIE_SAMESITE must be lax, strict or none, got {value!r}"
            )
        return mode

    @model_validator(mode="after")
    def _reject_cookie_delivery_that_browsers_will_drop(self) -> "Settings":
        """Fail on cookie settings the browser silently ignores.

        A SameSite=None cookie without Secure is discarded by every current
        browser. The symptom is a login that appears to work and a refresh that
        fails a minute later with "Missing or invalid refresh token" -- with no
        hint that the cookie was the reason. Failing at startup converts a
        confusing runtime failure into a clear one.
        """
        if self.refresh_token_delivery != "cookie":
            return self
        if self.refresh_token_cookie_samesite == "none" and not self.refresh_token_cookie_secure:
            raise ValueError(
                "REFRESH_TOKEN_COOKIE_SAMESITE=none requires "
                "REFRESH_TOKEN_COOKIE_SECURE=true; browsers drop the cookie "
                "otherwise and every refresh will fail."
            )
        return self


settings = Settings()