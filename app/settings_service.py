"""Global provider settings, encryption, and provider catalog clients."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any, Callable, Protocol
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken


class SettingsError(RuntimeError):
    """Base error for settings operations."""


class SettingsValidationError(SettingsError):
    """Raised when a setting value is not allowed."""


class ProviderCatalogError(SettingsError):
    """Raised when a provider catalog cannot be loaded."""


@dataclass
class GlobalSettings:
    openrouter_api_key_encrypted: str | None = None
    openrouter_model: str | None = None
    openrouter_video_model: str | None = None
    google_tts_voice_name: str = "ko-KR-Standard-A"
    video_resolution: str = "720p"
    video_max_duration_seconds: int = 30
    max_retries: int = 2
    mute_original_audio: bool = True


class SettingsRepository(Protocol):
    def get(self) -> GlobalSettings: ...

    def save(self, settings: GlobalSettings) -> GlobalSettings: ...


class InMemorySettingsRepository:
    """Small repository used by unit tests and local service composition."""

    def __init__(self, settings: GlobalSettings | None = None) -> None:
        self.settings = settings or GlobalSettings()

    def get(self) -> GlobalSettings:
        return self.settings

    def save(self, settings: GlobalSettings) -> GlobalSettings:
        self.settings = settings
        return settings


@dataclass(frozen=True)
class PublicSettings:
    api_key_configured: bool
    openrouter_model: str | None
    openrouter_video_model: str | None
    google_tts_voice_name: str
    video_resolution: str
    video_max_duration_seconds: int
    max_retries: int
    mute_original_audio: bool


class SettingsService:
    def __init__(self, repository: SettingsRepository, encryption_key: str) -> None:
        if not encryption_key:
            raise SettingsValidationError("SETTINGS_ENCRYPTION_KEY가 설정되지 않았습니다.")
        try:
            self.cipher = Fernet(encryption_key.encode())
        except (ValueError, TypeError) as error:
            raise SettingsValidationError(
                "SETTINGS_ENCRYPTION_KEY는 Fernet 키 형식이어야 합니다."
            ) from error
        self.repository = repository

    @staticmethod
    def test_key() -> str:
        return Fernet.generate_key().decode()

    def get_public(self) -> PublicSettings:
        settings = self.repository.get()
        return PublicSettings(
            api_key_configured=bool(settings.openrouter_api_key_encrypted),
            openrouter_model=settings.openrouter_model,
            openrouter_video_model=settings.openrouter_video_model,
            google_tts_voice_name=settings.google_tts_voice_name,
            video_resolution=settings.video_resolution,
            video_max_duration_seconds=settings.video_max_duration_seconds,
            max_retries=settings.max_retries,
            mute_original_audio=settings.mute_original_audio,
        )

    def get_openrouter_api_key(self) -> str | None:
        encrypted = self.repository.get().openrouter_api_key_encrypted
        if not encrypted:
            return None
        try:
            return self.cipher.decrypt(encrypted.encode()).decode()
        except InvalidToken as error:
            raise SettingsError("저장된 OpenRouter API Key를 복호화하지 못했습니다.") from error

    def update(self, values: dict[str, Any]) -> PublicSettings:
        self._validate(values)
        current = self.repository.get()
        updates = dict(values)
        api_key = updates.pop("openrouter_api_key", None)
        if api_key is not None:
            current = replace(
                current,
                openrouter_api_key_encrypted=self.cipher.encrypt(api_key.encode()).decode(),
            )
        self.repository.save(replace(current, **updates))
        return self.get_public()

    @staticmethod
    def _validate(values: dict[str, Any]) -> None:
        allowed = {
            "openrouter_api_key",
            "openrouter_model",
            "openrouter_video_model",
            "google_tts_voice_name",
            "video_resolution",
            "video_max_duration_seconds",
            "max_retries",
            "mute_original_audio",
        }
        unknown = set(values) - allowed
        if unknown:
            raise SettingsValidationError(f"지원하지 않는 설정입니다: {sorted(unknown)}")
        if "video_max_duration_seconds" in values and not 1 <= values["video_max_duration_seconds"] <= 30:
            raise SettingsValidationError("video_max_duration_seconds는 1~30초여야 합니다.")
        if "max_retries" in values and not 0 <= values["max_retries"] <= 5:
            raise SettingsValidationError("max_retries는 0~5회여야 합니다.")
        for field in ("openrouter_api_key", "openrouter_model", "openrouter_video_model", "google_tts_voice_name", "video_resolution"):
            if field in values and not isinstance(values[field], str):
                raise SettingsValidationError(f"{field}는 문자열이어야 합니다.")


class OpenRouterCatalogClient:
    def __init__(self, api_key: str, opener: Callable[..., Any] = urlopen) -> None:
        self.api_key = api_key
        self.opener = opener

    def list_models(self) -> list[dict[str, Any]]:
        if not self.api_key:
            raise ProviderCatalogError("OpenRouter API Key가 등록되지 않았습니다.")
        request = Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with self.opener(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise ProviderCatalogError("OpenRouter 모델 목록을 조회하지 못했습니다.") from error
        return [
            {key: item[key] for key in ("id", "name", "context_length") if key in item}
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]


class GoogleTTSCatalogClient:
    def __init__(self, client: Any | None = None) -> None:
        self.client = client or self._create_client()

    @staticmethod
    def _create_client() -> Any:
        try:
            from google.cloud import texttospeech
            return texttospeech.TextToSpeechClient()
        except Exception as error:
            raise ProviderCatalogError("Google TTS 인증 설정을 확인할 수 없습니다.") from error

    def list_voices(self, language_code: str = "ko-KR") -> list[dict[str, Any]]:
        try:
            response = self.client.list_voices(request={"language_code": language_code})
        except Exception as error:
            raise ProviderCatalogError("Google TTS Voice 목록을 조회하지 못했습니다.") from error
        return [
            {
                "name": voice.name,
                "language_codes": list(voice.language_codes),
                "ssml_gender": str(voice.ssml_gender),
            }
            for voice in response.voices
        ]

