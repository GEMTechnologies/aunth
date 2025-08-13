
from pydantic_settings import BaseSettings
from typing import List, Optional
import os

class Settings(BaseSettings):
    # Environment
    app_env: str = "development"
    debug: bool = True
    
    # API Configuration
    api_title: str = "Granada Authentication Service"
    api_version: str = "1.0.0"
    api_url: str = "http://0.0.0.0:8000"
    web_url: str = "http://0.0.0.0:3001"
    
    # Database
    database_url: str = "sqlite:///./granada_auth.db"
    redis_url: str = "redis://0.0.0.0:6379/0"
    database_echo: bool = False
    
    # JWT Settings
    jwt_secret: str = "your-very-secure-secret-key-change-in-production-minimum-32-chars"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "granada.auth"
    jwt_audience: List[str] = ["granada-web", "granada-api"]
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 30
    token_rotation: bool = True
    
    # Password Hashing (Argon2id)
    argon2_memory: int = 65536  # 64 MB
    argon2_time: int = 3        # 3 iterations
    argon2_parallelism: int = 2 # 2 threads
    
    # Email Configuration
    email_from: str = "noreply@granada.example"
    email_from_name: str = "Granada Auth"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_tls: bool = True
    smtp_ssl: bool = False
    
    # OAuth Providers
    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    facebook_client_id: str = ""
    facebook_client_secret: str = ""
    
    # SSO/SAML Configuration
    saml_sp_entity_id: str = "granada-auth"
    saml_sp_acs_url: str = ""
    saml_sp_x509_cert: str = ""
    saml_sp_private_key: str = ""
    
    # Security
    csrf_secret: str = "csrf-secret-key-change-in-production"
    cookie_domain: str = ".localhost"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    allowed_origins: List[str] = [
        "http://0.0.0.0:3001", 
        "http://localhost:3001",
        "http://0.0.0.0:3000",
        "http://localhost:3000"
    ]
    
    # Rate Limiting
    rate_limit_requests: int = 100
    rate_limit_window: int = 60  # seconds
    
    # Session Management
    session_timeout_hours: int = 24
    max_sessions_per_user: int = 10
    
    # Verification & Reset Tokens
    verification_token_ttl_hours: int = 24
    password_reset_token_ttl_hours: int = 1
    
    # File Upload
    max_upload_size: int = 10 * 1024 * 1024  # 10MB
    allowed_avatar_extensions: List[str] = [".jpg", ".jpeg", ".png", ".gif"]
    
    # Logging
    log_level: str = "INFO"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    class Config:
        env_file = ".env"
        case_sensitive = False

settings = Settings()
