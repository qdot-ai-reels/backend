import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str | None = None
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_REGION: str = "ap-northeast-2"
    S3_BUCKET_NAME: str
    SETTINGS_ENCRYPTION_KEY: str = ""
    
    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
