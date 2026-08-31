"""Generate product videos through OpenRouter's asynchronous video API."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.image_metadata import read_image_dimensions, validate_image_dimensions
from app.script_generator import (
    OpenRouterConfigurationError,
    OpenRouterRequestError,
    ScriptValidationError,
    validate_script_document,
)


DEFAULT_VIDEO_API_URL = "https://openrouter.ai/api/v1/videos"
DEFAULT_VIDEO_MODEL = "bytedance/seedance-2.0-mini"
DEFAULT_SUPPORTED_DURATIONS = tuple(range(4, 16))
logger = logging.getLogger(__name__)


class VideoGenerationError(RuntimeError):
    """Raised when a video generation job cannot be completed."""


class VideoGenerationTimeoutError(VideoGenerationError):
    """Raised when provider polling exceeds the configured wait window."""

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        last_status: str | None = None,
        poll_count: int | None = None,
        elapsed_seconds: float | None = None,
    ) -> None:
        self.job_id = job_id
        self.last_status = last_status
        self.poll_count = poll_count
        self.elapsed_seconds = elapsed_seconds
        super().__init__(message)


@dataclass(frozen=True)
class VideoGenerationRequest:
    script: Mapping[str, Any]
    image_url: str
    resolution: str = "720p"
    aspect_ratio: str = "9:16"
    generate_audio: bool = False
    influencer_image_url: str | None = None
    detail_image_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class VideoGenerationResult:
    job_id: str
    status: str
    video_url: str
    cost: float | None = None


def build_video_prompt(
    script: Mapping[str, Any],
    has_influencer_image: bool = False,
) -> str:
    """Convert a validated script document into a video-generation prompt."""
    scenes = script.get("scenes", [])
    scene_lines = []
    for scene in scenes:
        time_range = scene["time_range_sec"]
        start = time_range["start"]
        end = time_range["end"]
        scene_lines.append(
            f"{start}-{end}초: 화면={scene.get('visual', '')}"
        )

    reference_instruction = (
        "Use Image 1 as the AI influencer reference and Image 2 as the main product reference. "
        "Use any following images as additional product detail references. "
        "The AI influencer must be clearly visible on screen and actively promote the "
        "product in the generated video; do not replace the influencer with only a hand, "
        "finger, or an off-screen action. Preserve the "
        "influencer's identity and the product's shape, label, colors, and text. "
        if has_influencer_image
        else "Use the provided product image as the visual reference and preserve the product shape, label, colors, and text. "
    )
    return (
        "Create a vertical product advertisement video. "
        + reference_instruction
        + "Use a 9:16 aspect ratio, clean lighting, and simple transitions. "
        "Do not add subtitles, captions, prices, discounts, CTA text, or dialogue. "
        "Text animation will be added separately after video generation. "
        "Do not add unsupported product claims, extra products, new logos, "
        "or distorted text. Follow these script scenes:\n"
        + "Script context:\n"
        + convert_dict_to_formatted_text(script)
        + "\n"
        + "\n".join(scene_lines)
    )


def convert_dict_to_formatted_text(data: Mapping[str, Any]) -> str:
    """Format non-scene script fields for the video model prompt."""
    lines: list[str] = []

    def append_value(value: Any, indent: int) -> None:
        prefix = "  " * indent
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "scenes":
                    continue
                if isinstance(child, (Mapping, list)):
                    lines.append(f"{prefix}- {key}")
                    append_value(child, indent + 1)
                else:
                    lines.append(f"{prefix}- {key}: {child}")
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (Mapping, list)):
                    append_value(child, indent)
                else:
                    lines.append(f"{prefix}- {child}")

    append_value(data, 0)
    return "\n".join(lines)


class OpenRouterVideoClient:
    """Client for submitting and polling an OpenRouter video generation job."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_VIDEO_MODEL,
        api_url: str = DEFAULT_VIDEO_API_URL,
        timeout_seconds: int = 120,
        poll_interval_seconds: float = 5.0,
        # Poll every 5 seconds for up to 6 minutes before failing the job.
        max_poll_attempts: int = 72,
        supported_durations: tuple[int, ...] = DEFAULT_SUPPORTED_DURATIONS,
        supported_aspect_ratios: tuple[str, ...] = ("9:16",),
        supported_resolutions: tuple[str, ...] = ("480p", "720p"),
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        image_dimensions_reader: Callable[[str], tuple[int, int]] = read_image_dimensions,
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
        self.supported_aspect_ratios = supported_aspect_ratios
        self.supported_resolutions = supported_resolutions
        self.opener = opener
        self.sleeper = sleeper
        self.image_dimensions_reader = image_dimensions_reader

    @classmethod
    def from_env(cls) -> "OpenRouterVideoClient":
        return cls(
            api_key=os.getenv("OPENROUTER_VIDEO_API_KEY", ""),
            model=os.getenv("OPENROUTER_VIDEO_MODEL") or DEFAULT_VIDEO_MODEL,
            api_url=os.getenv("OPENROUTER_VIDEO_API_URL") or DEFAULT_VIDEO_API_URL,
            supported_durations=tuple(
                int(value)
                for value in (
                    os.getenv("OPENROUTER_VIDEO_SUPPORTED_DURATIONS")
                    or ",".join(str(value) for value in DEFAULT_SUPPORTED_DURATIONS)
                ).split(",")
            ),
        )

    def generate_video(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        if not self.api_key:
            raise OpenRouterConfigurationError(
                "OPENROUTER_VIDEO_API_KEY가 설정되지 않았습니다."
            )
        if request.aspect_ratio not in self.supported_aspect_ratios:
            raise VideoGenerationError(
                f"선택한 모델은 {request.aspect_ratio} 비율을 지원하지 않습니다. "
                f"지원 비율: {', '.join(self.supported_aspect_ratios)}"
            )
        if request.resolution not in self.supported_resolutions:
            raise VideoGenerationError(
                f"선택한 모델은 {request.resolution} 해상도를 지원하지 않습니다. "
                f"지원 해상도: {', '.join(self.supported_resolutions)}"
            )
        if not request.image_url:
            raise VideoGenerationError("영상 생성에는 상품 이미지 URL이 필요합니다.")
        try:
            validate_image_dimensions(
                request.image_url,
                dimensions_reader=self.image_dimensions_reader,
            )
            if request.influencer_image_url:
                validate_image_dimensions(
                    request.influencer_image_url,
                    dimensions_reader=self.image_dimensions_reader,
                )
            for detail_image_url in request.detail_image_urls:
                validate_image_dimensions(
                    detail_image_url,
                    dimensions_reader=self.image_dimensions_reader,
                )
        except ValueError as error:
            raise VideoGenerationError(f"입력 이미지를 사용할 수 없습니다: {error}") from error
        duration_seconds = self._validate_and_get_duration(request.script)
        if duration_seconds not in self.supported_durations:
            supported = ", ".join(str(value) for value in self.supported_durations)
            raise VideoGenerationError(
                f"선택한 모델은 {duration_seconds}초 영상을 지원하지 않습니다. "
                f"지원 길이: {supported}초"
            )

        submit_payload = {
            "model": self.model,
            "prompt": build_video_prompt(
                request.script,
                has_influencer_image=bool(request.influencer_image_url),
            ),
            "duration": duration_seconds,
            "resolution": request.resolution,
            "aspect_ratio": request.aspect_ratio,
            "generate_audio": request.generate_audio,
        }
        if request.influencer_image_url:
            submit_payload["input_references"] = [
                self._image_reference(request.influencer_image_url),
                self._image_reference(request.image_url),
                *(
                    self._image_reference(image_url)
                    for image_url in request.detail_image_urls
                ),
            ]
        else:
            submit_payload.update(_video_image_payload(self.model, request.image_url))

        submit_response = self._request_json(
            method="POST",
            url=self.api_url,
            payload=submit_payload,
        )
        job_id = submit_response.get("id")
        polling_url = submit_response.get("polling_url")
        if not job_id or not polling_url:
            raise VideoGenerationError("영상 생성 응답에 id 또는 polling_url이 없습니다.")

        started_at = time.monotonic()
        logger.info(
            "video generation submitted: job_id=%s model=%s duration=%ss "
            "resolution=%s aspect_ratio=%s",
            job_id,
            self.model,
            duration_seconds,
            request.resolution,
            request.aspect_ratio,
        )

        for attempt in range(self.max_poll_attempts):
            if attempt > 0:
                self.sleeper(self.poll_interval_seconds)
            try:
                result = self._request_json(method="GET", url=polling_url)
            except Exception:
                logger.exception(
                    "video generation polling request failed: job_id=%s poll=%s "
                    "elapsed=%.2fs",
                    job_id,
                    attempt + 1,
                    time.monotonic() - started_at,
                )
                raise
            raw_status = result.get("status")
            status = raw_status.lower() if isinstance(raw_status, str) else raw_status
            logger.info(
                "video generation polling: job_id=%s poll=%s/%s status=%s "
                "elapsed=%.2fs",
                job_id,
                attempt + 1,
                self.max_poll_attempts,
                status or "unknown",
                time.monotonic() - started_at,
            )
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
                raise VideoGenerationError(
                    f"영상 생성에 실패했습니다: "
                    f"{_video_failure_message(result, status)}"
                )

        elapsed_seconds = time.monotonic() - started_at
        logger.warning(
            "video generation polling timed out: job_id=%s last_status=%s "
            "polls=%s elapsed=%.2fs",
            job_id,
            status or "unknown",
            self.max_poll_attempts,
            elapsed_seconds,
        )
        raise VideoGenerationTimeoutError(
            "영상 생성 polling 시간이 초과되었습니다. "
            f"job_id={job_id}, 마지막 상태={status or 'unknown'}, "
            f"polling 횟수={self.max_poll_attempts}, "
            f"경과 시간={elapsed_seconds:.2f}초",
            job_id=job_id,
            last_status=status,
            poll_count=self.max_poll_attempts,
            elapsed_seconds=elapsed_seconds,
        )

    @staticmethod
    def _image_reference(image_url: str) -> dict[str, Any]:
        return {
            "type": "image_url",
            "image_url": {"url": image_url},
        }

    @staticmethod
    def _validate_and_get_duration(script: Mapping[str, Any]) -> int:
        try:
            validated_script = validate_script_document(script)
        except ScriptValidationError as error:
            raise VideoGenerationError(f"영상 생성용 스크립트가 올바르지 않습니다: {error}") from error

        last_scene_end = validated_script["scenes"][-1]["time_range_sec"]["end"]
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
            provider_detail = ""
            try:
                error_payload = json.loads(error.read().decode("utf-8"))
                if isinstance(error_payload, dict):
                    provider_error = error_payload.get("error")
                    if isinstance(provider_error, dict):
                        provider_detail = str(provider_error.get("message") or "").strip()
                    elif provider_error:
                        provider_detail = str(provider_error).strip()
            except (JSONDecodeError, UnicodeDecodeError):
                provider_detail = ""

            detail_suffix = f": {provider_detail[:500]}" if provider_detail else ""
            raise OpenRouterRequestError(
                f"OpenRouter 영상 요청이 거부되었습니다. HTTP {error.code}{detail_suffix}",
                status_code=error.code,
            ) from error
        except URLError as error:
            raise VideoGenerationError("OpenRouter 영상 API에 연결하지 못했습니다.") from error
        except (JSONDecodeError, UnicodeDecodeError) as error:
            raise VideoGenerationError("OpenRouter 영상 응답을 JSON으로 읽지 못했습니다.") from error

        if not isinstance(result, dict):
            raise VideoGenerationError("OpenRouter 영상 응답 형식이 올바르지 않습니다.")
        return result

def _video_failure_message(result: Mapping[str, Any], status: str) -> str:
    """Return a safe message for provider failures with varying JSON shapes."""
    provider_error = result.get("error")
    if isinstance(provider_error, Mapping):
        detail = provider_error.get("message") or provider_error.get("detail")
    elif provider_error is not None:
        detail = provider_error
    else:
        detail = result.get("message")

    message = str(detail).strip() if detail is not None else ""
    return message[:500] or status


def _video_image_payload(model: str, image_url: str) -> dict[str, list[dict[str, Any]]]:
    """Build the single-image input shape supported by the selected video model."""
    image = {
        "type": "image_url",
        "image_url": {"url": image_url},
    }
    if model.startswith("google/veo-"):
        return {
            "frame_images": [{**image, "frame_type": "first_frame"}],
        }
    return {"input_references": [image]}
