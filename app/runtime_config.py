"""Build generation clients from persisted settings with environment fallbacks."""

from __future__ import annotations

from app.script_generator import OpenRouterClient
from app.settings_service import (
    OpenRouterVideoCatalogClient,
    ProviderCatalogError,
    SettingsService,
    VideoModelCapabilities,
)
from app.tts_generator import GoogleTTSSettings
from app.video_generator import OpenRouterVideoClient


def build_script_client(service: SettingsService | None = None) -> OpenRouterClient:
    environment_client = OpenRouterClient.from_env()
    if service is None:
        return environment_client
    persisted = service.get_runtime_settings()
    return OpenRouterClient(
        api_key=service.get_openrouter_api_key() or environment_client.api_key,
        model=persisted.openrouter_model or environment_client.model,
        fallback_model=environment_client.fallback_model,
        api_url=environment_client.api_url,
        max_attempts=max(1, persisted.max_retries + 1),
    )


def get_video_model_capabilities(service: SettingsService | None = None) -> VideoModelCapabilities:
    environment_client = OpenRouterVideoClient.from_env()
    api_key = environment_client.api_key
    selected_model = environment_client.model
    if service is not None:
        persisted = service.get_runtime_settings()
        api_key = service.get_openrouter_api_key() or api_key
        selected_model = persisted.openrouter_video_model or selected_model

    catalog = OpenRouterVideoCatalogClient(api_key)
    capabilities = next(
        (item for item in catalog.list_models() if item.model_id == selected_model),
        None,
    )
    if capabilities is None:
        raise ProviderCatalogError(
            f"선택한 영상 모델의 capability 정보를 찾을 수 없습니다: {selected_model}"
        )
    return capabilities


def build_video_client(
    service: SettingsService | None = None,
    capabilities: VideoModelCapabilities | None = None,
) -> OpenRouterVideoClient:
    environment_client = OpenRouterVideoClient.from_env()
    api_key = environment_client.api_key
    model = environment_client.model
    supported_resolutions = environment_client.supported_resolutions
    if service is not None:
        persisted = service.get_runtime_settings()
        api_key = service.get_openrouter_api_key() or api_key
        model = persisted.openrouter_video_model or model
        if capabilities is None:
            supported_resolutions = (persisted.video_resolution,)

    return OpenRouterVideoClient(
        api_key=api_key,
        model=model,
        api_url=environment_client.api_url,
        supported_durations=(
            capabilities.supported_durations
            if capabilities
            else environment_client.supported_durations
        ),
        supported_aspect_ratios=(
            capabilities.supported_aspect_ratios
            if capabilities
            else environment_client.supported_aspect_ratios
        ),
        supported_resolutions=(
            capabilities.supported_resolutions
            if capabilities
            else supported_resolutions
        ),
    )


def build_tts_settings(service: SettingsService | None = None) -> GoogleTTSSettings:
    environment_settings = GoogleTTSSettings.from_env()
    if service is None:
        return environment_settings
    persisted = service.get_runtime_settings()
    return GoogleTTSSettings(
        language_code=environment_settings.language_code,
        voice_name=persisted.google_tts_voice_name or environment_settings.voice_name,
        syllables_per_second=environment_settings.syllables_per_second,
    )
