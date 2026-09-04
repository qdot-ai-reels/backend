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
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.image_metadata import (
    read_image_dimensions,
    read_image_format,
    validate_image_inputs,
    validate_normalized_influencer_references,
)
from app.prompt_versions import builtin_prompt_snapshot, render_prompt_template
from app.script_generator import (
    OpenRouterConfigurationError,
    OpenRouterRequestError,
    ScriptValidationError,
    validate_script_document,
)


DEFAULT_VIDEO_API_URL = "https://openrouter.ai/api/v1/videos"
DEFAULT_VIDEO_MODEL = "bytedance/seedance-2.0"
DEFAULT_SUPPORTED_DURATIONS = tuple(range(4, 16))
# OpenRouter accepts an exact WIDTHxHEIGHT `size` in place of the more
# ambiguous resolution/aspect pair. Only map combinations confirmed by the
# provider catalog; unknown combinations retain the generic API fields.
EXACT_VIDEO_SIZE_BY_RESOLUTION_AND_ASPECT_RATIO = {
    ("1080p", "9:16"): "1080x1920",
}
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
        polling_url: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.last_status = last_status
        self.poll_count = poll_count
        self.elapsed_seconds = elapsed_seconds
        self.polling_url = polling_url
        super().__init__(message)


@dataclass(frozen=True)
class VideoGenerationRequest:
    script: Mapping[str, Any]
    image_url: str
    # API orchestration selects 1080p explicitly. The reusable low-level value
    # stays 720p for callers that construct requests without capability lookup.
    resolution: str = "720p"
    aspect_ratio: str = "9:16"
    generate_audio: bool = False
    influencer_image_url: str | None = None
    influencer_image_urls: tuple[str, ...] = ()
    detail_image_urls: tuple[str, ...] = ()
    visual_mode: str = "product_only"
    prompt_templates: Mapping[str, str] | None = None


@dataclass(frozen=True)
class VideoGenerationResult:
    job_id: str
    status: str
    video_url: str
    cost: float | None = None


def build_video_prompt(
    script: Mapping[str, Any],
    has_influencer_image: bool = False,
    visual_mode: str = "product_only",
    *,
    resolution: str = "1080p",
    aspect_ratio: str = "9:16",
    prompt_templates: Mapping[str, str] | None = None,
) -> str:
    """Convert a validated script through one immutable prompt snapshot."""
    templates = (
        dict(prompt_templates)
        if prompt_templates is not None
        else builtin_prompt_snapshot().templates
    )
    common_values = {
        "script_visual_table": convert_dict_to_formatted_text(script),
        "duration_seconds": _script_duration_for_prompt(script),
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "visual_mode": visual_mode,
    }
    parts = [render_prompt_template(templates, "video_base", common_values)]
    if has_influencer_image:
        parts.append(
            render_prompt_template(
                templates,
                "video_identity_reference",
                common_values,
            )
        )
    if visual_mode == "generated_model":
        parts.append(
            render_prompt_template(
                templates,
                "video_generated_model",
                common_values,
            )
        )
    return "\n\n".join(part.strip() for part in parts if part.strip())


def _script_duration_for_prompt(script: Mapping[str, Any]) -> int | float | str:
    scenes = script.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return ""
    last = scenes[-1]
    time_range = last.get("time_range_sec") if isinstance(last, Mapping) else None
    if not isinstance(time_range, Mapping):
        return ""
    value = time_range.get("end")
    return value if isinstance(value, (int, float)) else ""


def convert_dict_to_formatted_text(data: Mapping[str, Any] | str) -> str:
    """Format the scene visual instructions as the documented Markdown table."""
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, Mapping):
        raise TypeError("스크립트는 JSON 객체 또는 dict여야 합니다.")
    scenes = data.get("scenes", [])
    if not isinstance(scenes, list) or not scenes:
        return ""

    rows = [
        "| Section | Time Range | Visual |",
        "| --- | --- | --- |",
    ]
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, Mapping):
            continue
        time_range = scene.get("time_range_sec", {})
        if not isinstance(time_range, Mapping):
            time_range = {}
        start = time_range.get("start", 0)
        end = time_range.get("end", 0)
        visual = str(scene.get("visual", "")).replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {index} | {start}s - {end}s | {visual} |")
    return "\n".join(rows)


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
        supported_resolutions: tuple[str, ...] = ("480p", "720p", "1080p", "4K"),
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        image_dimensions_reader: Callable[[str], tuple[int, int]] = read_image_dimensions,
        image_format_reader: Callable[[str], str] | None = None,
        on_submitted: Callable[[str, str], None] | None = None,
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
        self.image_format_reader = image_format_reader
        self.on_submitted = on_submitted

    @classmethod
    def from_env(cls) -> "OpenRouterVideoClient":
        from app.core.config import settings as app_settings

        return cls(
            api_key=(
                os.getenv("OPENROUTER_VIDEO_API_KEY")
                or os.getenv("OPENROUTER_API_KEY")
                or app_settings.OPENROUTER_API_KEY
                or ""
            ),
            model=os.getenv("OPENROUTER_VIDEO_MODEL") or DEFAULT_VIDEO_MODEL,
            api_url=os.getenv("OPENROUTER_VIDEO_API_URL") or DEFAULT_VIDEO_API_URL,
            image_format_reader=read_image_format,
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
                "OPENROUTER_VIDEO_API_KEY 또는 OPENROUTER_API_KEY가 설정되지 않았습니다."
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
        influencer_image_urls = request.influencer_image_urls or (
            (request.influencer_image_url,) if request.influencer_image_url else ()
        )
        if request.visual_mode not in {
            "product_only",
            "model_included",
            "generated_model",
        }:
            raise VideoGenerationError(
                f"지원하지 않는 visual_mode입니다: {request.visual_mode}"
            )
        if request.visual_mode == "generated_model" and influencer_image_urls:
            raise VideoGenerationError(
                "generated_model 모드에는 인물 reference를 함께 보낼 수 없습니다."
            )
        try:
            influencer_image_urls = validate_normalized_influencer_references(
                influencer_image_urls,
                dimensions_reader=self.image_dimensions_reader,
            )
            valid_detail_image_urls = validate_image_inputs(
                image_url=request.image_url,
                influencer_image_urls=influencer_image_urls,
                detail_image_urls=request.detail_image_urls,
                dimensions_reader=self.image_dimensions_reader,
                format_reader=self.image_format_reader,
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

        output_dimensions = _video_output_dimensions_payload(
            request.resolution,
            request.aspect_ratio,
        )
        submit_payload = {
            "model": self.model,
            "prompt": build_video_prompt(
                request.script,
                has_influencer_image=bool(influencer_image_urls),
                visual_mode=request.visual_mode,
                resolution=request.resolution,
                aspect_ratio=request.aspect_ratio,
                prompt_templates=request.prompt_templates,
            ),
            "duration": duration_seconds,
            "generate_audio": request.generate_audio,
            **output_dimensions,
        }
        submit_payload.update(
            _video_image_payload(
                self.model,
                request.image_url,
                influencer_image_urls=influencer_image_urls,
                detail_image_urls=valid_detail_image_urls,
                generated_model=request.visual_mode == "generated_model",
            )
        )

        diagnostics = _video_request_diagnostics(
            submit_payload, influencer_count=len(influencer_image_urls)
        )
        logger.warning(
            "video generation request: model=%s duration=%ss resolution=%s "
            "aspect_ratio=%s size=%s reference_count=%s reference_order=%s "
            "reference_domains=%s",
            self.model,
            duration_seconds,
            request.resolution,
            request.aspect_ratio,
            output_dimensions.get("size") or "resolution+aspect",
            diagnostics["reference_count"],
            diagnostics["reference_order"],
            diagnostics["reference_domains"],
        )
        submit_response = self._request_json(
            method="POST",
            url=self.api_url,
            payload=submit_payload,
        )
        job_id = submit_response.get("id")
        polling_url = submit_response.get("polling_url")
        if not job_id or not polling_url:
            raise VideoGenerationError("영상 생성 응답에 id 또는 polling_url이 없습니다.")
        if self.on_submitted is not None:
            self.on_submitted(str(job_id), str(polling_url))

        started_at = time.monotonic()
        logger.info(
            "video generation submitted: job_id=%s model=%s duration=%ss "
            "resolution=%s aspect_ratio=%s size=%s",
            job_id,
            self.model,
            duration_seconds,
            request.resolution,
            request.aspect_ratio,
            output_dimensions.get("size") or "resolution+aspect",
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
            polling_url=polling_url,
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
        if (
            isinstance(last_scene_end, bool)
            or not isinstance(last_scene_end, (int, float))
            or not float(last_scene_end).is_integer()
            or last_scene_end < 1
        ):
            raise VideoGenerationError("스크립트의 마지막 장면 종료 시간은 양의 정수여야 합니다.")
        return int(last_scene_end)

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

            logger.warning(
                "video provider request failed: method=%s status=%s message=%s",
                method,
                error.code,
                provider_detail[:500] or "(no provider message)",
            )
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


def _video_output_dimensions_payload(
    resolution: str,
    aspect_ratio: str,
) -> dict[str, str]:
    """Prefer a catalog-confirmed exact canvas, with a compatible fallback."""
    exact_size = EXACT_VIDEO_SIZE_BY_RESOLUTION_AND_ASPECT_RATIO.get(
        (resolution, aspect_ratio)
    )
    if exact_size:
        return {"size": exact_size}
    return {
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
    }


def _video_image_payload(
    model: str,
    image_url: str,
    *,
    influencer_image_urls: tuple[str, ...] = (),
    detail_image_urls: tuple[str, ...] = (),
    generated_model: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Build only reference shapes known to be supported by each model family."""
    image = {
        "type": "image_url",
        "image_url": {"url": image_url},
    }
    if model.startswith("google/veo-"):
        if influencer_image_urls:
            raise VideoGenerationError(
                "선택한 Veo 모델에는 인물 identity reference와 상품 frame을 함께 "
                "전달하지 않습니다. Seedance 2.0을 선택하거나 인물 레퍼런스를 제거하세요."
            )
        return {
            "frame_images": [{**image, "frame_type": "first_frame"}],
        }
    if model.startswith("alibaba/wan-"):
        if influencer_image_urls:
            raise VideoGenerationError(
                "선택한 Wan 모델은 이 파이프라인의 다중 identity reference 계약을 "
                "지원하지 않습니다. Seedance 2.0을 선택하세요."
            )
        return {"frame_images": [{**image, "frame_type": "first_frame"}]}
    if model.startswith("openai/sora-"):
        raise VideoGenerationError(
            "선택한 Sora 모델은 현재 파이프라인의 상품 이미지 reference 입력을 "
            "지원하지 않습니다."
        )

    if (
        not influencer_image_urls
        and not generated_model
        and model.startswith("bytedance/seedance-")
    ):
        return {"frame_images": [{**image, "frame_type": "first_frame"}]}

    references = [
        *(
            {
                "type": "image_url",
                "image_url": {"url": influencer_url},
            }
            for influencer_url in influencer_image_urls[:2]
        ),
        image,
        *(
            {
                "type": "image_url",
                "image_url": {"url": detail_url},
            }
            for detail_url in detail_image_urls[: max(0, 2 - len(influencer_image_urls))]
        ),
    ]
    return {"input_references": references}


def _video_request_diagnostics(
    payload: Mapping[str, Any], *, influencer_count: int = 0
) -> dict[str, Any]:
    """Summarize image inputs without logging credentials or full URLs."""
    if "frame_images" in payload:
        references = payload.get("frame_images") or []
        reference_order = ["product"] * len(references)
    else:
        references = payload.get("input_references") or []
        reference_order = (
            ["influencer"] * min(influencer_count, len(references))
            + (["product"] if len(references) > influencer_count else [])
        )
        reference_order += ["detail"] * (len(references) - len(reference_order))

    domains = []
    for reference in references:
        image = reference.get("image_url") if isinstance(reference, Mapping) else None
        url = image.get("url") if isinstance(image, Mapping) else None
        hostname = urlsplit(str(url)).hostname if url else None
        domains.append(hostname or "unknown")

    return {
        "reference_count": len(references),
        "reference_order": ",".join(reference_order) or "none",
        "reference_domains": ",".join(domains) or "none",
    }
