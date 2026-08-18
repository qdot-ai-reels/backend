from collections.abc import Generator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.settings_service import (
    GoogleTTSCatalogClient,
    InMemorySettingsRepository,
    OpenRouterCatalogClient,
    ProviderCatalogError,
    SettingsError,
    SettingsService,
    SettingsValidationError,
)


router = APIRouter()
_repository = InMemorySettingsRepository()


class SettingsUpdateBody(BaseModel):
    openrouter_api_key: str | None = Field(default=None, min_length=1)
    openrouter_model: str | None = None
    openrouter_video_model: str | None = None
    google_tts_voice_name: str | None = None
    video_resolution: str | None = None
    video_max_duration_seconds: int | None = Field(default=None, ge=1, le=30)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    mute_original_audio: bool | None = None


def get_settings_repository() -> Generator[SettingsService, None, None]:
    from app.core.config import settings
    from app.db import SQLAlchemySettingsRepository, SessionLocal, init_db

    init_db()
    session = SessionLocal()
    try:
        yield SettingsService(
            SQLAlchemySettingsRepository(session), settings.SETTINGS_ENCRYPTION_KEY
        )
    finally:
        session.close()


def get_openrouter_catalog() -> OpenRouterCatalogClient:
    from app.core.config import settings
    from app.db import SQLAlchemySettingsRepository, SessionLocal, init_db

    init_db()
    session = SessionLocal()
    try:
        service = SettingsService(
            SQLAlchemySettingsRepository(session), settings.SETTINGS_ENCRYPTION_KEY
        )
        return OpenRouterCatalogClient(service.get_openrouter_api_key() or "")
    finally:
        session.close()


def get_google_tts_catalog() -> GoogleTTSCatalogClient:
    return GoogleTTSCatalogClient()


@router.get("/settings")
def get_settings(service: SettingsService = Depends(get_settings_repository)) -> dict[str, Any]:
    try:
        return service.get_public().__dict__
    except SettingsError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.put("/settings")
def update_settings(
    body: SettingsUpdateBody,
    service: SettingsService = Depends(get_settings_repository),
) -> dict[str, Any]:
    try:
        return service.update(body.model_dump(exclude_none=True)).__dict__
    except SettingsValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SettingsError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.get("/settings/openrouter/models")
def list_openrouter_models(catalog: OpenRouterCatalogClient = Depends(get_openrouter_catalog)) -> list[dict[str, Any]]:
    try:
        return catalog.list_models()
    except ProviderCatalogError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/settings/google-tts/voices")
def list_google_tts_voices(catalog: GoogleTTSCatalogClient = Depends(get_google_tts_catalog)) -> list[dict[str, Any]]:
    try:
        return catalog.list_voices()
    except ProviderCatalogError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
