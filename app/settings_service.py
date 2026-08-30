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
    openrouter_script_api_key_encrypted: str | None = None
    openrouter_tts_api_key_encrypted: str | None = None
    openrouter_video_api_key_encrypted: str | None = None
    openrouter_script_model: str | None = None
    openrouter_tts_model: str | None = None
    openrouter_video_model: str | None = None
    openrouter_tts_voice: str = ""
    video_min_resolution: str = "720p"
    video_max_resolution: str = "1080p"
    video_max_duration_seconds: int = 15
    script_generation_retries: int = 2
    video_generation_retries: int = 2
    media_combine_retries: int = 3
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
    script_api_key_configured: bool
    tts_api_key_configured: bool
    video_api_key_configured: bool
    openrouter_script_model: str | None
    openrouter_tts_model: str | None
    openrouter_video_model: str | None
    openrouter_tts_voice: str
    video_min_resolution: str
    video_max_resolution: str
    video_max_duration_seconds: int
    script_generation_retries: int
    video_generation_retries: int
    media_combine_retries: int
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
            script_api_key_configured=bool(settings.openrouter_script_api_key_encrypted),
            tts_api_key_configured=bool(settings.openrouter_tts_api_key_encrypted),
            video_api_key_configured=bool(settings.openrouter_video_api_key_encrypted),
            openrouter_script_model=settings.openrouter_script_model,
            openrouter_tts_model=settings.openrouter_tts_model,
            openrouter_video_model=settings.openrouter_video_model,
            openrouter_tts_voice=settings.openrouter_tts_voice,
            video_min_resolution=settings.video_min_resolution,
            video_max_resolution=settings.video_max_resolution,
            video_max_duration_seconds=settings.video_max_duration_seconds,
            script_generation_retries=settings.script_generation_retries,
            video_generation_retries=settings.video_generation_retries,
            media_combine_retries=settings.media_combine_retries,
            mute_original_audio=settings.mute_original_audio,
        )

    def _decrypt_key(self, encrypted: str | None, name: str) -> str | None:
        if not encrypted:
            return None
        try:
            return self.cipher.decrypt(encrypted.encode()).decode()
        except InvalidToken as error:
            raise SettingsError(f"저장된 {name} API Key를 복호화하지 못했습니다.") from error

    def get_script_api_key(self) -> str | None:
        return self._decrypt_key(
            self.repository.get().openrouter_script_api_key_encrypted,
            "스크립트용 OpenRouter",
        )

    def get_tts_api_key(self) -> str | None:
        return self._decrypt_key(
            self.repository.get().openrouter_tts_api_key_encrypted,
            "TTS용 OpenRouter",
        )

    def get_video_api_key(self) -> str | None:
        return self._decrypt_key(
            self.repository.get().openrouter_video_api_key_encrypted,
            "영상용 OpenRouter",
        )

    def get_runtime_settings(self) -> GlobalSettings:
        """Return the current settings for a newly started generation job."""
        return self.repository.get()

    def update(self, values: dict[str, Any]) -> PublicSettings:
        self._validate(values)
        current = self.repository.get()
        self._validate_resolution_range(
            values.get("video_min_resolution", current.video_min_resolution),
            values.get("video_max_resolution", current.video_max_resolution),
        )
        updates = dict(values)
        key_fields = {
            "openrouter_script_api_key": "openrouter_script_api_key_encrypted",
            "openrouter_tts_api_key": "openrouter_tts_api_key_encrypted",
            "openrouter_video_api_key": "openrouter_video_api_key_encrypted",
        }
        for input_field, stored_field in key_fields.items():
            api_key = updates.pop(input_field, None)
            if api_key is not None:
                current = replace(
                    current,
                    **{stored_field: self.cipher.encrypt(api_key.encode()).decode()},
                )
        self.repository.save(replace(current, **updates))
        return self.get_public()

    @staticmethod
    def _validate(values: dict[str, Any]) -> None:
        allowed = {
            "openrouter_script_api_key",
            "openrouter_tts_api_key",
            "openrouter_video_api_key",
            "openrouter_script_model",
            "openrouter_tts_model",
            "openrouter_video_model",
            "openrouter_tts_voice",
            "video_min_resolution",
            "video_max_resolution",
            "video_max_duration_seconds",
            "script_generation_retries",
            "video_generation_retries",
            "media_combine_retries",
            "mute_original_audio",
        }
        unknown = set(values) - allowed
        if unknown:
            raise SettingsValidationError(f"지원하지 않는 설정입니다: {sorted(unknown)}")
        if "video_max_duration_seconds" in values and not 1 <= values["video_max_duration_seconds"] <= 30:
            raise SettingsValidationError("video_max_duration_seconds는 1~30초여야 합니다.")
        for field in (
            "script_generation_retries",
            "video_generation_retries",
            "media_combine_retries",
        ):
            if field in values and not 0 <= values[field] <= 5:
                raise SettingsValidationError(f"{field}는 0~5회여야 합니다.")
        if "mute_original_audio" in values and not isinstance(values["mute_original_audio"], bool):
            raise SettingsValidationError("mute_original_audio는 boolean이어야 합니다.")
        for field in (
            "openrouter_script_api_key",
            "openrouter_tts_api_key",
            "openrouter_video_api_key",
            "openrouter_script_model",
            "openrouter_tts_model",
            "openrouter_video_model",
            "openrouter_tts_voice",
            "video_min_resolution",
            "video_max_resolution",
        ):
            if field in values and not isinstance(values[field], str):
                raise SettingsValidationError(f"{field}는 문자열이어야 합니다.")

        if "video_min_resolution" in values or "video_max_resolution" in values:
            min_resolution = values.get("video_min_resolution")
            max_resolution = values.get("video_max_resolution")
            if min_resolution is not None and max_resolution is not None:
                SettingsService._validate_resolution_range(min_resolution, max_resolution)

    @staticmethod
    def _validate_resolution_range(min_resolution: str, max_resolution: str) -> None:
        def parse(value: str, field: str) -> int:
            if not value.endswith("p") or not value[:-1].isdigit() or int(value[:-1]) <= 0:
                raise SettingsValidationError(f"{field}는 720p와 같은 형식이어야 합니다.")
            return int(value[:-1])

        min_value = parse(min_resolution, "video_min_resolution")
        max_value = parse(max_resolution, "video_max_resolution")
        if min_value > max_value:
            raise SettingsValidationError(
                "video_min_resolution은 video_max_resolution보다 클 수 없습니다."
            )


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


@dataclass(frozen=True)
class VideoModelCapabilities:
    model_id: str
    name: str | None
    supported_durations: tuple[int, ...]
    supported_aspect_ratios: tuple[str, ...]
    supported_resolutions: tuple[str, ...]
    generate_audio: bool


class OpenRouterVideoCatalogClient:
    """Client for OpenRouter's video model capability catalog."""

    def __init__(self, api_key: str = "", opener: Callable[..., Any] = urlopen) -> None:
        self.api_key = api_key
        self.opener = opener

    def list_models(self) -> list[VideoModelCapabilities]:
        request = Request(
            "https://openrouter.ai/api/v1/videos/models",
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
        )
        try:
            with self.opener(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise ProviderCatalogError("OpenRouter 영상 모델 목록을 조회하지 못했습니다.") from error

        return [
            VideoModelCapabilities(
                model_id=item["id"],
                name=item.get("name"),
                supported_durations=tuple(item.get("supported_durations") or ()),
                supported_aspect_ratios=tuple(item.get("supported_aspect_ratios") or ()),
                supported_resolutions=tuple(item.get("supported_resolutions") or ()),
                generate_audio=bool(item.get("generate_audio", False)),
            )
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]


class OpenRouterTTSCatalogClient:
    def __init__(self, api_key: str, opener: Callable[..., Any] = urlopen) -> None:
        self.api_key = api_key
        self.opener = opener

    def _list_models_payload(self) -> list[dict[str, Any]]:
        if not self.api_key:
            raise ProviderCatalogError("OpenRouter TTS API Key가 등록되지 않았습니다.")
        request = Request(
            "https://openrouter.ai/api/v1/models?output_modalities=speech",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with self.opener(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise ProviderCatalogError("OpenRouter TTS 모델 목록을 조회하지 못했습니다.") from error

        return [
            item
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {key: item[key] for key in ("id", "name", "pricing", "supported_voices") if key in item}
            for item in self._list_models_payload()
        ]

    def list_voices(self) -> list[dict[str, Any]]:
        models = self._list_models_payload()

        voices = []
        for model in models:
            for voice in model.get("supported_voices") or []:
                voices.append({"model": model["id"], "name": voice})
        return voices
