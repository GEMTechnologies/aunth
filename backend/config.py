
from pydantic_settings import BaseSettings
from typing import List

class Settings(BaseSettings):
    # App
    app_env: str = "dev"
    api_url: str = "http://localhost:8000"
    web_url: str = "http://localhost:5173"
    
    # Database
    database_url: str = "sqlite:///./granada_auth.db"
    redis_url: str = "redis://localhost:6379/0"
    
    # JWT
    jwt_secret: str = "your-secret-key-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "granada.auth"
    jwt_audience: List[str] = ["granada-web", "granada-api"]
    access_token_ttl_min: int = 10
    refresh_token_ttl_days: int = 60
    token_rotation: bool = True
    
    # Argon2
    argon2_memory: int = 65536
    argon2_time: int = 3
    argon2_parallelism: int = 2
    
    # Email
    email_from: str = "noreply@granada.example"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_pass: str = ""
    
    # Security
    csrf_secret: str = "csrf-secret-change-in-production"
    cookie_domain: str = ".localhost"
    allowed_origins: List[str] = ["http://localhost:5173", "http://localhost:3000"]
    
    class Config:
        env_file = ".env"

settings = Settings()
