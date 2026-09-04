import os
from pathlib import Path

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str | None = None
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "ap-northeast-2"
    S3_BUCKET_NAME: str = ""
    SETTINGS_ENCRYPTION_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    
    class Config:
        _backend_root = Path(__file__).resolve().parents[2]
        env_file = (
            str(_backend_root.parent / ".env"),
            str(_backend_root / ".env"),
        )
        extra = "ignore"

settings = Settings()
