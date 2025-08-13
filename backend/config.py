from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Optional

class Settings(BaseSettings):
    database_url: str = "sqlite:///./auth.db"
    jwt_secret: str = "change_this_in_production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    app_url: Optional[str] = "http://localhost:5173"
    backend_url: Optional[str] = "http://localhost:8000"

    class Config:
        env_file = ".env"
        env_prefix = ""
        case_sensitive = False

settings = Settings()
