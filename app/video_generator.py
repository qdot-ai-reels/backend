"""Generate product videos through OpenRouter's asynchronous video API."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.script_generator import (
    OpenRouterConfigurationError,
    OpenRouterRequestError,
    ScriptValidationError,
    validate_script_document,
)


DEFAULT_VIDEO_API_URL = "https://openrouter.ai/api/v1/videos"
DEFAULT_VIDEO_MODEL = "google/veo-3.1-lite"
DEFAULT_SUPPORTED_DURATIONS = (4, 6, 8)


class VideoGenerationError(RuntimeError):
    """Raised when a video generation job cannot be completed."""


@dataclass(frozen=True)
class VideoGenerationRequest:
    script: Mapping[str, Any]
    image_url: str
    resolution: str = "720p"
    aspect_ratio: str = "9:16"
    generate_audio: bool = False


@dataclass(frozen=True)
class VideoGenerationResult:
    job_id: str
    status: str
    video_url: str
    cost: float | None = None


def build_video_prompt(script: Mapping[str, Any]) -> str:
    """Convert a validated script document into a video-generation prompt."""
    scenes = script.get("scenes", [])
    scene_lines = []
    for scene in scenes:
        start, end = scene["time_range_sec"]
        scene_lines.append(
            f"{start}-{end}초: 화면={scene.get('visual', '')}; "
            f"자막={scene.get('subtitle', '')}; "
            f"내레이션={scene.get('voiceover', '')}"
        )

    return (
        "Create a vertical product advertisement video. Use the provided product image "
        "as the visual reference and preserve the product shape, label, colors, and text. "
        "Use a 9:16 aspect ratio, clean lighting, and simple transitions. "
        "Do not add unsupported product claims, extra products, people, hands, new logos, "
        "or distorted text. Follow these script scenes:\n"
        + "\n".join(scene_lines)
    )


class OpenRouterVideoClient:
    """Client for submitting and polling an OpenRouter video generation job."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_VIDEO_MODEL,
        api_url: str = DEFAULT_VIDEO_API_URL,
        timeout_seconds: int = 120,
        poll_interval_seconds: float = 5.0,
        max_poll_attempts: int = 24,
        supported_durations: tuple[int, ...] = DEFAULT_SUPPORTED_DURATIONS,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_poll_attempts < 1:
            raise ValueError("max_poll_attempts는 1 이상이어야 합니다.")
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_poll_attempts = max_poll_attempts
        self.supported_durations = supported_durations
        self.opener = opener
        self.sleeper = sleeper

    @classmethod
    def from_env(cls) -> "OpenRouterVideoClient":
        return cls(
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            model=os.getenv("OPENROUTER_VIDEO_MODEL") or DEFAULT_VIDEO_MODEL,
            api_url=os.getenv("OPENROUTER_VIDEO_API_URL") or DEFAULT_VIDEO_API_URL,
            supported_durations=tuple(
                int(value)
                for value in (os.getenv("OPENROUTER_VIDEO_SUPPORTED_DURATIONS") or "4,6,8").split(",")
            ),
        )

    def generate_video(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        if not self.api_key:
            raise OpenRouterConfigurationError("OPENROUTER_API_KEY가 설정되지 않았습니다.")
        if request.aspect_ratio != "9:16":
            raise VideoGenerationError("영상 생성 요청의 aspect_ratio는 9:16이어야 합니다.")
        if not request.image_url:
            raise VideoGenerationError("영상 생성에는 상품 이미지 URL이 필요합니다.")
        duration_seconds = self._validate_and_get_duration(request.script)
        if duration_seconds not in self.supported_durations:
            supported = ", ".join(str(value) for value in self.supported_durations)
            raise VideoGenerationError(
                f"선택한 모델은 {duration_seconds}초 영상을 지원하지 않습니다. "
                f"지원 길이: {supported}초"
            )

        submit_response = self._request_json(
            method="POST",
            url=self.api_url,
            payload={
                "model": self.model,
                "prompt": build_video_prompt(request.script),
                "duration": duration_seconds,
                "resolution": request.resolution,
                "aspect_ratio": request.aspect_ratio,
                "generate_audio": request.generate_audio,
                "frame_images": [{
                    "type": "image_url",
                    "image_url": {"url": request.image_url},
                    "frame_type": "first_frame",
                }],
            },
        )
        job_id = submit_response.get("id")
        polling_url = submit_response.get("polling_url")
        if not job_id or not polling_url:
            raise VideoGenerationError("영상 생성 응답에 id 또는 polling_url이 없습니다.")

        for attempt in range(self.max_poll_attempts):
            if attempt > 0:
                self.sleeper(self.poll_interval_seconds)
            result = self._request_json(method="GET", url=polling_url)
            status = result.get("status")
            if status == "completed":
                urls = result.get("unsigned_urls") or []
                if not urls:
                    raise VideoGenerationError("완료된 영상의 URL이 없습니다.")
                usage = result.get("usage") or {}
                return VideoGenerationResult(
                    job_id=job_id,
                    status=status,
                    video_url=urls[0],
                    cost=usage.get("cost"),
                )
            if status in {"failed", "error", "cancelled"}:
                error = result.get("error") or {}
                raise VideoGenerationError(
                    f"영상 생성에 실패했습니다: {error.get('message', status)}"
                )

        raise VideoGenerationError("영상 생성 polling 시간이 초과되었습니다.")

    @staticmethod
    def _validate_and_get_duration(script: Mapping[str, Any]) -> int:
        try:
            validated_script = validate_script_document(script)
        except ScriptValidationError as error:
            raise VideoGenerationError(f"영상 생성용 스크립트가 올바르지 않습니다: {error}") from error

        last_scene_end = validated_script["scenes"][-1]["time_range_sec"][1]
        if not isinstance(last_scene_end, int) or last_scene_end < 1:
            raise VideoGenerationError("스크립트의 마지막 장면 종료 시간은 양의 정수여야 합니다.")
        return last_scene_end

    def _request_json(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        http_request = Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener(http_request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise OpenRouterRequestError(
                f"OpenRouter 영상 요청이 거부되었습니다. HTTP {error.code}",
                status_code=error.code,
            ) from error
        except URLError as error:
            raise VideoGenerationError("OpenRouter 영상 API에 연결하지 못했습니다.") from error
        except (JSONDecodeError, UnicodeDecodeError) as error:
            raise VideoGenerationError("OpenRouter 영상 응답을 JSON으로 읽지 못했습니다.") from error

        if not isinstance(result, dict):
            raise VideoGenerationError("OpenRouter 영상 응답 형식이 올바르지 않습니다.")
        return result
