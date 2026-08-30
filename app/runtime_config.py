"""Build generation clients from persisted settings with environment fallbacks."""

from __future__ import annotations

import os

from app.script_generator import OpenRouterClient, select_supported_video_duration
from app.settings_service import (
    OpenRouterVideoCatalogClient,
    ProviderCatalogError,
    SettingsService,
    VideoModelCapabilities,
)
from app.tts_generator import OpenRouterTTSSettings
from app.video_generator import OpenRouterVideoClient


def build_script_client(service: SettingsService | None = None) -> OpenRouterClient:
    environment_client = OpenRouterClient.from_env()
    if service is None:
        return environment_client
    persisted = service.get_runtime_settings()
    return OpenRouterClient(
        api_key=(
            service.get_script_api_key()
            or os.getenv("OPENROUTER_SCRIPT_API_KEY")
            or environment_client.api_key
        ),
        model=persisted.openrouter_script_model or environment_client.model,
        fallback_model=environment_client.fallback_model,
        api_url=environment_client.api_url,
        max_attempts=max(1, persisted.script_generation_retries + 1),
    )


def get_video_model_capabilities(service: SettingsService | None = None) -> VideoModelCapabilities:
    environment_client = OpenRouterVideoClient.from_env()
    api_key = os.getenv("OPENROUTER_VIDEO_API_KEY", "")
    if service is not None:
        api_key = service.get_video_api_key() or api_key
    selected_model = environment_client.model
    if service is not None:
        persisted = service.get_runtime_settings()
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


def resolve_script_generation_duration(
    max_duration_seconds: int, service: SettingsService | None = None
) -> tuple[int, tuple[int, ...] | None]:
    """Resolve a script duration against the selected video's live capabilities."""
    api_key = os.getenv("OPENROUTER_VIDEO_API_KEY", "")
    if service is not None:
        api_key = service.get_video_api_key() or api_key

    # Keep script-only local tests usable when no video provider is configured.
    if not api_key:
        return max_duration_seconds, None

    capabilities = get_video_model_capabilities(service)
    duration = select_supported_video_duration(
        max_duration_seconds, capabilities.supported_durations
    )
    return duration, capabilities.supported_durations


def build_video_client(
    service: SettingsService | None = None,
    capabilities: VideoModelCapabilities | None = None,
    max_poll_attempts: int | None = None,
) -> OpenRouterVideoClient:
    environment_client = OpenRouterVideoClient.from_env()
    api_key = environment_client.api_key
    model = environment_client.model
    supported_resolutions = environment_client.supported_resolutions
    if service is not None:
        persisted = service.get_runtime_settings()
        api_key = service.get_video_api_key() or api_key
        model = persisted.openrouter_video_model or model
        if capabilities is None:
            supported_resolutions = (persisted.video_max_resolution,)

    client_kwargs = {
        "api_key": api_key,
        "model": model,
        "api_url": environment_client.api_url,
        "supported_durations": (
            capabilities.supported_durations
            if capabilities
            else environment_client.supported_durations
        ),
        "supported_aspect_ratios": (
            capabilities.supported_aspect_ratios
            if capabilities
            else environment_client.supported_aspect_ratios
        ),
        "supported_resolutions": (
            capabilities.supported_resolutions
            if capabilities
            else supported_resolutions
        ),
    }
    if max_poll_attempts is not None:
        client_kwargs["max_poll_attempts"] = max_poll_attempts

    return OpenRouterVideoClient(
        **client_kwargs,
    )


def build_tts_settings(service: SettingsService | None = None) -> OpenRouterTTSSettings:
    environment_settings = OpenRouterTTSSettings.from_env()
    if service is None:
        return environment_settings
    persisted = service.get_runtime_settings()
    return OpenRouterTTSSettings(
        api_key=environment_settings.api_key
        or (service.get_tts_api_key() if service is not None else "")
        or "",
        model=(
            persisted.openrouter_tts_model
            if service is not None and persisted.openrouter_tts_model
            else environment_settings.model
        ),
        language_code=environment_settings.language_code,
        voice_name=persisted.openrouter_tts_voice or environment_settings.voice_name,
        syllables_per_second=environment_settings.syllables_per_second,
    )
