"""Build generation clients from persisted settings with environment fallbacks."""

from __future__ import annotations

from app.script_generator import OpenRouterClient
from app.settings_service import SettingsService
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


def build_video_client(service: SettingsService | None = None) -> OpenRouterVideoClient:
    environment_client = OpenRouterVideoClient.from_env()
    if service is None:
        return environment_client
    persisted = service.get_runtime_settings()
    return OpenRouterVideoClient(
        api_key=service.get_openrouter_api_key() or environment_client.api_key,
        model=persisted.openrouter_video_model or environment_client.model,
        api_url=environment_client.api_url,
        supported_durations=environment_client.supported_durations,
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

