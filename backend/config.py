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

from typing import List

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


settings = Settings()