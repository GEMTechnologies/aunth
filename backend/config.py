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
    redis_url: str = "redis://0.0.0.0:6379/0"

    # -- URLs -------------------------------------------------------------
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
    # boots and runs with JEV_ENABLED=false.
    decision_gateway_enabled: bool = True
    decision_provider: str = "rules"
    jev_enabled: bool = False
    # SHADOW is the only stage that cannot influence an action, and the brief is
    # explicit that it comes first: record what Jev would have decided, compare
    # it with Granada's own answer, and enable authority only on measured
    # reliability rather than on a vendor benchmark.
    decision_rollout_stage: str = "SHADOW"
    decision_autonomy: str = "MONITOR_ONLY"
    decision_cache_ttl_seconds: int = 3600
    decision_timeout_seconds: int = 20
    # Off by default. A decision fingerprint plus references is enough to audit
    # with, and the state may contain donor and beneficiary content.
    decision_store_state: bool = False

    # -- TypeSafe / Jev ---------------------------------------------------
    # The key is read here and nowhere else, and is registered as a secret with
    # the logging layer so it cannot reach a log line. Only backend decision
    # workers may use it: never a frontend bundle, never a database column.
    typesafe_api_key: str = ""
    typesafe_base_url: str = ""
    typesafe_default_model: str = "jev-latest"

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
        allowed = {"rules", "jev", "llm", "hybrid"}
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

    @field_validator("decision_autonomy")
    @classmethod
    def _known_autonomy(cls, value: str) -> str:
        autonomy = (value or "MONITOR_ONLY").strip().upper()
        allowed = {"MONITOR_ONLY", "DRAFT_ONLY", "AUTO_ROUTINE", "AUTOPILOT_WITH_GATES"}
        if autonomy not in allowed:
            raise ValueError(
                f"decision_autonomy must be one of {sorted(allowed)}, got {value!r}"
            )
        return autonomy

    @model_validator(mode="after")
    def _warn_when_jev_is_enabled_without_a_key(self) -> "Settings":
        """Warn, and deliberately do not raise.

        The application must boot with no TypeSafe key - the brief requires it,
        and the rules provider answers every decision type Granada initially
        needs. A missing key is a degraded configuration, not a broken one, and
        turning it into a startup failure would make an optional vendor into a
        hard dependency.
        """
        if self.jev_enabled and not self.typesafe_api_key:
            import logging

            logging.getLogger(__name__).warning(
                "JEV_ENABLED is true but TYPESAFE_API_KEY is empty; the Jev "
                "provider will be skipped and the decision chain will fall back. "
                "Granada continues to operate."
            )
        return self

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