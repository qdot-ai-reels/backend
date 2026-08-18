from collections.abc import Generator
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.settings_service import (
    GoogleTTSCatalogClient,
    OpenRouterCatalogClient,
    OpenRouterVideoCatalogClient,
    ProviderCatalogError,
    SettingsError,
    SettingsService,
    SettingsValidationError,
)


router = APIRouter()


class SettingsUpdateBody(BaseModel):
    openrouter_api_key: str | None = Field(default=None, min_length=1)
    openrouter_model: str | None = None
    openrouter_video_model: str | None = None
    google_tts_voice_name: str | None = None
    video_min_resolution: str | None = None
    video_max_resolution: str | None = None
    video_max_duration_seconds: int | None = Field(default=None, ge=1, le=30)
    script_generation_retries: int | None = Field(default=None, ge=0, le=5)
    video_generation_retries: int | None = Field(default=None, ge=0, le=5)
    media_combine_retries: int | None = Field(default=None, ge=0, le=5)
    mute_original_audio: bool | None = None


def get_settings_repository() -> Generator[SettingsService, None, None]:
    from app.core.config import settings
    from app.db import SQLAlchemySettingsRepository, SessionLocal

    session = SessionLocal()
    try:
        yield SettingsService(
            SQLAlchemySettingsRepository(session), settings.SETTINGS_ENCRYPTION_KEY
        )
    finally:
        session.close()


def get_optional_settings_repository() -> Generator[SettingsService | None, None, None]:
    """Use DB settings when encryption is configured; otherwise keep env fallback."""
    from app.core.config import settings

    if not settings.SETTINGS_ENCRYPTION_KEY:
        yield None
        return
    yield from get_settings_repository()


def _get_openrouter_api_key_for_catalog() -> str:
    from app.core.config import settings
    from app.db import SQLAlchemySettingsRepository, SessionLocal

    environment_api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not settings.SETTINGS_ENCRYPTION_KEY:
        return environment_api_key

    session = SessionLocal()
    try:
        service = SettingsService(
            SQLAlchemySettingsRepository(session), settings.SETTINGS_ENCRYPTION_KEY
        )
        return service.get_openrouter_api_key() or environment_api_key
    finally:
        session.close()


def get_openrouter_catalog() -> OpenRouterCatalogClient:
    return OpenRouterCatalogClient(_get_openrouter_api_key_for_catalog())


def get_google_tts_catalog() -> GoogleTTSCatalogClient:
    return GoogleTTSCatalogClient()


def get_openrouter_video_catalog() -> OpenRouterVideoCatalogClient:
    return OpenRouterVideoCatalogClient(_get_openrouter_api_key_for_catalog())


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
    video_catalog: OpenRouterVideoCatalogClient = Depends(get_openrouter_video_catalog),
) -> dict[str, Any]:
    try:
        values = body.model_dump(exclude_none=True)
        current_settings = service.get_runtime_settings()
        selected_video_model = values.get(
            "openrouter_video_model", current_settings.openrouter_video_model
        )
        if (
            selected_video_model
            and isinstance(video_catalog, OpenRouterVideoCatalogClient)
            and any(
                field in values
                for field in (
                    "openrouter_video_model",
                    "video_min_resolution",
                    "video_max_resolution",
                    "video_max_duration_seconds",
                )
            )
        ):
            capabilities = next(
                (
                    item
                    for item in video_catalog.list_models()
                    if item.model_id == selected_video_model
                ),
                None,
            )
            if capabilities is None:
                raise SettingsValidationError(
                    f"지원하지 않는 OpenRouter 영상 모델입니다: {selected_video_model}"
                )
            _apply_video_capability_defaults(values, current_settings, capabilities)
        return service.update(values).__dict__
    except ProviderCatalogError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except SettingsValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SettingsError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


def _apply_video_capability_defaults(
    values: dict[str, Any],
    current: Any,
    capabilities: Any,
) -> None:
    """Keep persisted video settings within the selected model's capabilities."""
    if capabilities.supported_durations:
        requested_duration = values.get(
            "video_max_duration_seconds", current.video_max_duration_seconds
        )
        values["video_max_duration_seconds"] = min(
            requested_duration, max(capabilities.supported_durations)
        )

@router.get("/settings/openrouter/models")
def list_openrouter_models(catalog: OpenRouterCatalogClient = Depends(get_openrouter_catalog)) -> list[dict[str, Any]]:
    try:
        return catalog.list_models()
    except ProviderCatalogError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/settings/openrouter/video-models")
def list_openrouter_video_models(
    catalog: OpenRouterVideoCatalogClient = Depends(get_openrouter_video_catalog),
) -> list[dict[str, Any]]:
    try:
        return [capability.__dict__ for capability in catalog.list_models()]
    except ProviderCatalogError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/settings/google-tts/voices")
def list_google_tts_voices(catalog: GoogleTTSCatalogClient = Depends(get_google_tts_catalog)) -> list[dict[str, Any]]:
    try:
        return catalog.list_voices()
    except ProviderCatalogError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
