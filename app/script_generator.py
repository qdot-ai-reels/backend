"""Generate structured short-form scripts through the OpenRouter chat API."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from json import JSONDecodeError
from copy import deepcopy
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.generation_templates import (
    GenerationTemplateError,
    normalize_generated_script_to_plan,
)
from app.prompt_versions import (
    builtin_prompt_snapshot,
    render_creative_brief,
    render_prompt_template,
)


DEFAULT_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-5.4-mini"
DEFAULT_SYLLABLES_PER_SECOND = 4.5
DEFAULT_CTA_ACTION = "상품 자세히 보기"
MAX_SCRIPT_DURATION_SECONDS = 30
logger = logging.getLogger(__name__)


SCRIPT_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["meta", "product", "customer", "ads", "video", "scenes", "etc"],
    "properties": {
        "meta": {
            "type": "object",
            "additionalProperties": False,
            "required": ["output_format_version", "language"],
            "properties": {
                "output_format_version": {"type": "string"},
                "language": {"type": "string"},
            },
        },
        "product": {
            "type": "object",
            "additionalProperties": False,
            "required": ["usp"],
            "properties": {
                "usp": {
                    "type": "string",
                    "description": "상품의 Unique Selling Point. 입력된 값은 그대로 사용하고, 없으면 상품정보로 추론",
                },
            },
        },
        "customer": {
            "type": "object",
            "additionalProperties": False,
            "required": ["main_target", "pain_point"],
            "properties": {
                "main_target": {
                    "type": ["string", "null"],
                    "description": "상품의 메인 타겟 고객",
                },
                "pain_point": {
                    "type": ["string", "null"],
                    "description": "타겟 고객의 고민",
                },
            },
        },
        "ads": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "goal",
                "cta_action",
                "channel_platform",
                "ad_planner",
                "speaker",
                "main_target",
            ],
            "properties": {
                "goal": {"type": ["string", "null"], "description": "광고 목표. 없으면 null"},
                "cta_action": {"type": "string", "description": "광고를 본 사용자가 할 행동"},
                "channel_platform": {"type": "string", "description": "광고가 업로드되는 채널"},
                "main_target": {"type": ["string", "null"], "description": "광고의 메인 타겟"},
                "ad_planner": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["persona"],
                    "properties": {
                        "persona": {
                            "type": ["string", "null"],
                            "description": "광고 영상 기획자의 특성. 없으면 null",
                        }
                    },
                },
                "speaker": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["persona", "tone"],
                    "properties": {
                        "persona": {
                            "type": ["string", "null"],
                            "description": "화자의 성격·특성·가치관. 외형 묘사는 금지하며 없으면 null",
                        },
                        "tone": {
                            "type": ["string", "null"],
                            "description": "광고 영상에 어울리는 화자의 말투. 없으면 null",
                        },
                    },
                },
            },
        },
        "video": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "video_duration",
                "required_scenes_elements",
                "forbidden_scenes_elements",
            ],
            "properties": {
                "video_duration": {"type": "string", "description": "생성하는 광고 영상의 길이"},
                "required_scenes_elements": {
                    "type": ["string", "null"],
                    "description": "반드시 포함할 시각·소품·연출 요소. 없으면 null",
                },
                "forbidden_scenes_elements": {
                    "type": ["string", "null"],
                    "description": "절대 포함하지 않을 시각·소품·연출 요소. 없으면 null",
                },
            },
        },
        "scenes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "section",
                    "time_range_sec",
                    "visual",
                    "auditory",
                    "intent",
                    "notes",
                ],
                "properties": {
                    "section": {"type": "string", "description": "영상의 부분 파트"},
                    "time_range_sec": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start", "end"],
                        "properties": {
                            "start": {
                                "type": "number",
                                "minimum": 0,
                                "description": "장면 시작 시간(초)",
                            },
                            "end": {
                                "type": "number",
                                "exclusiveMinimum": 0,
                                "description": "장면 종료 시간(초)",
                            },
                        },
                    },
                    "visual": {
                        "type": "string",
                        "maxLength": 99,
                        "description": "영상 장면의 시각 요소 설명. 글자 수 100자 미만",
                    },
                    "auditory": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["subtitle", "voiceover"],
                        "properties": {
                            "subtitle": {
                                "type": ["string", "null"],
                                "description": "영상 장면의 Caption(텍스트 애니메이션)",
                            },
                            "voiceover": {
                                "type": ["string", "null"],
                                "description": "영상 장면의 목소리 추가",
                            },
                        },
                    },
                    "intent": {
                        "type": "string",
                        "description": "영상 장면의 연출 의도 설명",
                    },
                    "notes": {
                        "type": ["string", "null"],
                        "description": "영상 장면의 기타 추가 설명. 없으면 null",
                    },
                },
            },
        },
        "etc": {
            "type": "object",
            "additionalProperties": False,
            "required": ["additional_information", "video_ads_methodology"],
            "properties": {
                "additional_information": {
                    "type": ["string", "null"],
                    "description": "사용자가 입력한 추가사항. 없으면 null",
                },
                "video_ads_methodology": {
                    "type": ["string", "null"],
                    "description": "광고 영상에 활용할 방법론. 없으면 null",
                },
            },
        },
    },
}


class OpenRouterError(RuntimeError):
    """Base error for OpenRouter request and response failures."""


class OpenRouterConfigurationError(OpenRouterError):
    """Raised when required OpenRouter configuration is missing."""


class OpenRouterRequestError(OpenRouterError):
    """Raised when OpenRouter rejects or cannot receive a request."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _http_error_detail(error: HTTPError) -> str:
    """Extract a short, non-sensitive provider error summary for diagnostics."""
    try:
        raw_body = error.read().decode("utf-8", errors="replace")
        payload = json.loads(raw_body)
    except (OSError, JSONDecodeError, UnicodeDecodeError):
        return ""

    provider_error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(provider_error, dict):
        fields = []
        for key in ("code", "message", "type"):
            value = provider_error.get(key)
            if isinstance(value, str) and value.strip():
                fields.append(f"{key}={value.strip()[:300]}")
        return ", ".join(fields)
    if isinstance(provider_error, str):
        return provider_error.strip()[:300]
    return ""


def _http_error_request_id(error: HTTPError) -> str | None:
    """Read a provider request identifier without exposing request content."""
    for header_name in ("x-request-id", "x-openrouter-request-id", "request-id"):
        value = error.headers.get(header_name) if error.headers else None
        if value and value.strip():
            return value.strip()[:200]
    return None


class ScriptValidationError(OpenRouterError):
    """Raised when the model response is not a usable script document."""


class ScriptDialogueLengthError(ScriptValidationError):
    """Raised when a scene dialogue exceeds its expected speaking time."""

    def __init__(self, scene_number: int, max_syllables: int, actual_syllables: int) -> None:
        self.scene_number = scene_number
        self.max_syllables = max_syllables
        self.actual_syllables = actual_syllables
        super().__init__(
            f"{scene_number}번째 scene의 대사가 너무 깁니다. "
            f"허용 음절 수: {max_syllables}개, 실제 음절 수: {actual_syllables}개"
        )


@dataclass(frozen=True)
class ScriptGenerationRequest:
    product: Mapping[str, Any]
    image_url: str | None = None
    reviews: list[Any] | None = None
    custom_prompt: str | None = None
    max_duration_seconds: int = 30
    channel: str = "Instagram Reels"
    target_audience: str = "육아에 관심 있는 보호자"
    supported_video_durations: tuple[int, ...] | None = None
    retry_instruction: str | None = None
    template_scene_plan: tuple[Mapping[str, Any], ...] | None = None
    prompt_templates: Mapping[str, str] | None = None
    creative_brief: str | None = None
    retry_error: str | None = None
    resolution: str = "1080p"
    aspect_ratio: str = "9:16"
    visual_mode: str = "product_only"

    def __post_init__(self) -> None:
        if not 1 <= self.max_duration_seconds <= MAX_SCRIPT_DURATION_SECONDS:
            raise ValueError(
                f"max_duration_seconds는 1초 이상 {MAX_SCRIPT_DURATION_SECONDS}초 이하여야 합니다."
            )


def select_supported_video_duration(
    max_duration_seconds: int, supported_durations: tuple[int, ...] | None
) -> int:
    """Choose the longest duration supported without exceeding the configured cap."""
    if not supported_durations:
        return max_duration_seconds

    candidates = sorted(
        {duration for duration in supported_durations if 1 <= duration <= max_duration_seconds}
    )
    if not candidates:
        supported = ", ".join(str(duration) for duration in sorted(set(supported_durations)))
        raise ValueError(
            f"설정된 최대 영상 길이({max_duration_seconds}초) 이하로 사용할 수 있는 "
            f"모델 지원 길이가 없습니다. 지원 길이: {supported}초"
        )
    return candidates[-1]


def prepare_product_for_prompt(product: Mapping[str, Any]) -> dict[str, Any]:
    """Remove fields that the team decided not to use for script generation."""
    return {
        key: value
        for key, value in product.items()
        if key != "social_posts"
    }


def _format_product_prompt_value(value: Any) -> str:
    """Match the Colab prompt's direct Python f-string conversion."""
    return str(value)


def build_product_prompt_fields(
    product: Mapping[str, Any], reviews: list[Any] | None
) -> str:
    """Render the product section in the same order and shape as the Colab prompt."""
    selling_point = product.get("selling_point", product.get("selling_points"))
    usp = product.get("usp")
    curator_pitch = product.get("curator_pitch")
    hashtags = product.get("hashtags")
    description_text = product.get("description_text")
    detail_info = product.get("detail_info")
    product_reviews = product.get("reviews", reviews or [])
    return "\n".join(
        [
            f"- Selling Point: {_format_product_prompt_value(selling_point)}",
            f"- USP(Unique Selling Point): {_format_product_prompt_value(usp)}",
            f"- Curator Pitch: {_format_product_prompt_value(curator_pitch)}",
            f"- Hashtags: {_format_product_prompt_value(hashtags)}",
            f"- Description Text: {_format_product_prompt_value(description_text)}",
            f"- Detail Info: {_format_product_prompt_value(detail_info)}",
            f"- Reviews: {_format_product_prompt_value(product_reviews)}",
        ]
    )


def extract_cta_action(custom_prompt: str) -> str:
    """Extract a line or inline CTA without ever emitting a literal null."""
    match = re.search(
        r"(?i)(?<![A-Za-z])cta(?:\s+action)?\s*[:：]\s*([^\n,;|]*)",
        custom_prompt,
    )
    if match is None:
        return DEFAULT_CTA_ACTION
    value = match.group(1).strip().strip("\"'`- ")
    if value.casefold() in {"", "null", "none", "n/a", "na", "없음", "미정"}:
        return DEFAULT_CTA_ACTION
    return value


def build_script_prompt(request: ScriptGenerationRequest) -> str:
    """Build from the pinned six-template snapshot or bundled legacy fallback."""
    templates = (
        dict(request.prompt_templates)
        if request.prompt_templates is not None
        else builtin_prompt_snapshot().templates
    )
    product_context = build_product_prompt_fields(request.product, request.reviews)
    custom_prompt = request.custom_prompt.strip() if request.custom_prompt else ""
    cta_action = extract_cta_action(custom_prompt)
    common_values: dict[str, Any] = {
        "channel": request.channel,
        "target_audience": request.target_audience,
        "duration_seconds": request.max_duration_seconds,
        "resolution": request.resolution,
        "aspect_ratio": request.aspect_ratio,
        "visual_mode": request.visual_mode,
        "must_include": "",
        "must_exclude": "",
        "extra_details": "",
        "retry_instruction": "",
    }
    if request.creative_brief is not None:
        creative_brief = request.creative_brief
    else:
        creative_brief = render_creative_brief(
            templates,
            advertising_purpose=None,
            cta=cta_action,
            visual_mode=request.visual_mode,
            must_include=None,
            must_exclude=None,
            extra_details=custom_prompt or None,
            common_values=common_values,
        )

    plan_lines = []
    if request.template_scene_plan:
        plan_lines = [
            (
                f"- {scene['label']}: {scene['start_seconds']:g}~"
                f"{scene['end_seconds']:g}초"
            )
            for scene in request.template_scene_plan
        ]
    template_scene_plan = "\n".join(plan_lines) if plan_lines else "없음"

    retry_parts = []
    if request.retry_error:
        retry_values = {
            **common_values,
            "retry_error": json.dumps(request.retry_error, ensure_ascii=False),
        }
        retry_parts.append(
            render_prompt_template(templates, "script_tts_repair", retry_values)
        )
    if request.retry_instruction and request.retry_instruction.strip():
        retry_parts.append(request.retry_instruction.strip())
    common_values["retry_instruction"] = "\n\n".join(retry_parts)

    return render_prompt_template(
        templates,
        "script_generation",
        {
            **common_values,
            "product_context": product_context,
            "creative_brief": creative_brief,
            "template_scene_plan": template_scene_plan,
        },
    )


def build_script_message_content(
    request: ScriptGenerationRequest,
    prompt: str,
) -> str | list[dict[str, Any]]:
    """Build OpenRouter content, adding the product image when supplied."""
    if not request.image_url:
        return prompt
    return [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": request.image_url}},
    ]


def extract_script_json(content: str) -> dict[str, Any]:
    """Extract the first JSON object from a model response."""
    if not isinstance(content, str) or not content.strip():
        raise ScriptValidationError("모델 응답에 스크립트 내용이 없습니다.")

    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            document, _ = decoder.raw_decode(cleaned[index:])
        except JSONDecodeError:
            continue
        if isinstance(document, dict):
            return document

    raise ScriptValidationError("모델 응답에서 JSON 객체를 찾지 못했습니다.")


def validate_script_document(
    document: Mapping[str, Any],
    max_duration_seconds: int | None = None,
    *,
    validate_dialogue: bool = True,
) -> dict[str, Any]:
    """Validate the minimum contract consumed by later video tasks."""
    if not isinstance(document, Mapping):
        raise ScriptValidationError("스크립트 응답은 JSON 객체여야 합니다.")

    # Keep previously generated documents readable while new API responses use
    # the current PRD schema below.
    if "product" not in document:
        return _validate_legacy_script_document(
            document,
            max_duration_seconds,
            validate_dialogue=validate_dialogue,
        )

    scenes = document.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ScriptValidationError("스크립트에는 하나 이상의 scenes가 필요합니다.")

    meta = document.get("meta")
    if not isinstance(meta, Mapping):
        raise ScriptValidationError("스크립트의 meta가 필요합니다.")
    for field in ("output_format_version", "language"):
        if not isinstance(meta.get(field), str) or not meta[field].strip():
            raise ScriptValidationError(f"스크립트의 meta.{field}가 필요합니다.")

    product = document.get("product")
    if not isinstance(product, Mapping) or not isinstance(product.get("usp"), str):
        raise ScriptValidationError("스크립트의 product.usp가 필요합니다.")

    for parent, fields in {
        "customer": ("main_target", "pain_point"),
        "ads": (
            "goal",
            "cta_action",
            "channel_platform",
            "ad_planner",
            "speaker",
            "main_target",
        ),
        "video": (
            "video_duration",
            "required_scenes_elements",
            "forbidden_scenes_elements",
        ),
        "etc": ("additional_information", "video_ads_methodology"),
    }.items():
        value = document.get(parent)
        if not isinstance(value, Mapping):
            raise ScriptValidationError(f"스크립트의 {parent}는 객체여야 합니다.")
        for field in fields:
            if field not in value:
                raise ScriptValidationError(f"스크립트의 {parent}.{field}가 필요합니다.")

    ads = document["ads"]
    for parent, fields in {"ad_planner": ("persona",), "speaker": ("persona", "tone")}.items():
        value = ads.get(parent)
        if not isinstance(value, Mapping):
            raise ScriptValidationError(f"스크립트의 ads.{parent}는 객체여야 합니다.")
        for field in fields:
            if field not in value:
                raise ScriptValidationError(f"스크립트의 ads.{parent}.{field}가 필요합니다.")

    previous_end = 0.0
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, Mapping):
            raise ScriptValidationError(f"{index}번째 scene이 JSON 객체가 아닙니다.")
        time_range = scene.get("time_range_sec")
        if not isinstance(time_range, Mapping):
            raise ScriptValidationError(
                f"{index}번째 scene의 time_range_sec는 객체여야 합니다."
            )
        start = time_range.get("start")
        end = time_range.get("end")
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or start < 0
            or end <= start
            or start < previous_end
            or (max_duration_seconds is not None and end > max_duration_seconds)
        ):
            raise ScriptValidationError(
                f"{index}번째 scene의 time_range_sec가 올바르지 않습니다."
            )
        required_fields = ("section", "visual", "auditory", "intent", "notes")
        if any(field not in scene for field in required_fields):
            raise ScriptValidationError(
                f"{index}번째 scene에 필수 출력 필드가 누락되었습니다."
            )
        if not isinstance(scene.get("section"), str) or not scene["section"].strip():
            raise ScriptValidationError(f"{index}번째 scene의 section이 필요합니다.")
        if not isinstance(scene.get("visual"), str) or not scene["visual"].strip():
            raise ScriptValidationError(
                f"{index}번째 scene의 visual이 필요합니다."
            )
        if len(scene["visual"]) >= 100:
            raise ScriptValidationError(
                f"{index}번째 scene의 visual은 100자 미만이어야 합니다."
            )
        if not isinstance(scene.get("intent"), str) or not scene["intent"].strip():
            raise ScriptValidationError(f"{index}번째 scene의 intent가 필요합니다.")
        auditory = scene.get("auditory")
        if not isinstance(auditory, Mapping):
            raise ScriptValidationError(f"{index}번째 scene의 auditory가 필요합니다.")
        if "voiceover" not in auditory:
            raise ScriptValidationError(f"{index}번째 scene의 voiceover가 필요합니다.")
        subtitle = auditory.get("subtitle")
        if subtitle is not None and not isinstance(subtitle, str):
            raise ScriptValidationError(f"{index}번째 scene의 subtitle이 필요합니다.")
        if auditory.get("voiceover") is not None and not isinstance(auditory["voiceover"], str):
            raise ScriptValidationError(
                f"{index}번째 scene의 voiceover는 문자열 또는 null이어야 합니다."
            )
        if scene.get("notes") is not None and not isinstance(scene["notes"], str):
            raise ScriptValidationError(
                f"{index}번째 scene의 notes는 문자열 또는 null이어야 합니다."
            )
        previous_end = float(end)

    if validate_dialogue:
        validate_dialogue_lengths(document)

    return dict(document)


def _validate_legacy_script_document(
    document: Mapping[str, Any],
    max_duration_seconds: int | None = None,
    *,
    validate_dialogue: bool = True,
) -> dict[str, Any]:
    """Read older saved scripts while the PRD schema is being rolled out."""
    scenes = document.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ScriptValidationError("스크립트에는 하나 이상의 scenes가 필요합니다.")

    meta = document.get("meta")
    if not isinstance(meta, Mapping):
        raise ScriptValidationError("스크립트의 meta가 필요합니다.")
    for field in ("output_format_version", "framework", "language"):
        if not isinstance(meta.get(field), str) or not meta[field].strip():
            raise ScriptValidationError(f"스크립트의 meta.{field}가 필요합니다.")

    summary = document.get("summary")
    if not isinstance(summary, Mapping):
        raise ScriptValidationError("스크립트의 summary는 객체여야 합니다.")
    for field in (
        "main_target",
        "pain_point",
        "product_usp",
        "key_message",
        "tone_and_manner",
    ):
        if not isinstance(summary.get(field), str):
            raise ScriptValidationError(f"스크립트의 summary.{field}가 필요합니다.")

    compliance_notes = document.get("compliance_notes")
    if not isinstance(compliance_notes, Mapping):
        raise ScriptValidationError("스크립트의 compliance_notes는 객체여야 합니다.")
    for field in ("avoid", "focus"):
        if not isinstance(compliance_notes.get(field), list):
            raise ScriptValidationError(
                f"스크립트의 compliance_notes.{field}가 필요합니다."
            )

    previous_end = 0.0
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, Mapping):
            raise ScriptValidationError(f"{index}번째 scene이 JSON 객체가 아닙니다.")
        time_range = scene.get("time_range_sec")
        if not isinstance(time_range, Mapping):
            raise ScriptValidationError(
                f"{index}번째 scene의 time_range_sec는 객체여야 합니다."
            )
        start = time_range.get("start")
        end = time_range.get("end")
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or start < 0
            or end <= start
            or start < previous_end
            or (max_duration_seconds is not None and end > max_duration_seconds)
        ):
            raise ScriptValidationError(
                f"{index}번째 scene의 time_range_sec가 올바르지 않습니다."
            )
        required_fields = ("scene_name", "visual", "auditory", "notes")
        if any(field not in scene for field in required_fields):
            raise ScriptValidationError(
                f"{index}번째 scene에 필수 출력 필드가 누락되었습니다."
            )
        if not isinstance(scene.get("scene_name"), str) or not scene["scene_name"].strip():
            raise ScriptValidationError(f"{index}번째 scene의 scene_name이 필요합니다.")
        if not isinstance(scene.get("visual"), str) or not scene["visual"].strip():
            raise ScriptValidationError(f"{index}번째 scene의 visual이 필요합니다.")
        auditory = scene.get("auditory")
        if not isinstance(auditory, Mapping):
            raise ScriptValidationError(f"{index}번째 scene의 auditory가 필요합니다.")
        if "voiceover" not in auditory:
            raise ScriptValidationError(f"{index}번째 scene의 voiceover가 필요합니다.")
        subtitle = auditory.get("subtitle")
        if subtitle is not None and not isinstance(subtitle, str):
            raise ScriptValidationError(f"{index}번째 scene의 subtitle이 필요합니다.")
        if auditory.get("voiceover") is not None and not isinstance(auditory["voiceover"], str):
            raise ScriptValidationError(
                f"{index}번째 scene의 voiceover는 문자열 또는 null이어야 합니다."
            )
        if not isinstance(scene.get("notes"), str):
            raise ScriptValidationError(f"{index}번째 scene의 notes가 필요합니다.")
        previous_end = float(end)

    if validate_dialogue:
        validate_dialogue_lengths(document)
    return dict(document)


def count_speech_syllables(text: str) -> int:
    """Count spoken characters while ignoring whitespace and punctuation."""
    return sum(1 for character in text if character.isalnum())


def normalize_script_subtitles(document: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize captions and ensure every scene has production-safe text."""
    normalized = deepcopy(document)
    scenes = normalized.get("scenes") or []
    product = normalized.get("product") or {}
    summary = normalized.get("summary") or {}
    ads = normalized.get("ads") or {}

    def usable_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or value.casefold() in {"null", "none", "n/a", "na"}:
            return None
        return value

    for index, scene in enumerate(scenes):
        auditory = scene.get("auditory") or {}
        subtitle = auditory.get("subtitle")
        if isinstance(subtitle, str):
            subtitle = subtitle.replace("\\r\\n", "\n").replace("\\n", "\n").strip()
        if not subtitle:
            voiceover = usable_text(auditory.get("voiceover"))
            cta = usable_text(ads.get("cta_action")) if index == len(scenes) - 1 else None
            usp = usable_text(product.get("usp")) if isinstance(product, Mapping) else None
            key_message = (
                usable_text(summary.get("key_message"))
                if isinstance(summary, Mapping)
                else None
            )
            subtitle = voiceover or cta or usp or key_message or "상품 정보"
        auditory["subtitle"] = subtitle
    return normalized


def truncate_voiceover_at_boundary(text: str, max_syllables: int) -> str | None:
    """Return an existing factual prefix at a word/clause boundary, or silence."""
    if max_syllables < 1:
        return None
    spoken = 0
    end_index = 0
    for index, character in enumerate(text):
        if character.isalnum():
            if spoken >= max_syllables:
                break
            spoken += 1
        end_index = index + 1
    else:
        return text.strip() or None

    raw_prefix = text[:end_index]
    prefix = raw_prefix.strip()
    if end_index < len(text) and text[end_index].isalnum():
        if not raw_prefix or not raw_prefix[-1].isspace():
            boundary_indexes = [
                index + 1
                for index, character in enumerate(prefix)
                if character.isspace() or character in ".!?。！？…,;:，、"
            ]
            if not boundary_indexes:
                return None
            prefix = prefix[: boundary_indexes[-1]].strip()
    prefix = prefix.rstrip(",;:，、 ")
    return prefix or None


def fit_script_dialogue_lengths(
    document: Mapping[str, Any],
    syllables_per_second: float = DEFAULT_SYLLABLES_PER_SECOND,
) -> dict[str, Any]:
    """Fit overlong voiceovers locally after strict structural validation."""
    if syllables_per_second <= 0:
        raise ValueError("syllables_per_second는 0보다 커야 합니다.")
    fitted = normalize_script_subtitles(document)
    for index, scene in enumerate(fitted.get("scenes") or [], start=1):
        auditory = scene["auditory"]
        voiceover = auditory.get("voiceover")
        if not isinstance(voiceover, str) or not voiceover.strip():
            continue
        time_range = scene["time_range_sec"]
        max_syllables = max(
            1,
            int((time_range["end"] - time_range["start"]) * syllables_per_second),
        )
        actual_syllables = count_speech_syllables(voiceover)
        if actual_syllables <= max_syllables:
            continue
        fitted_voiceover = truncate_voiceover_at_boundary(
            voiceover.strip(),
            max_syllables,
        )
        auditory["voiceover"] = fitted_voiceover
        logger.warning(
            "script dialogue fitted locally: scene=%s action=%s max_syllables=%s "
            "original_syllables=%s fitted_syllables=%s",
            index,
            "shortened" if fitted_voiceover else "silenced",
            max_syllables,
            actual_syllables,
            count_speech_syllables(fitted_voiceover or ""),
        )
    return fitted


def validate_dialogue_lengths(
    document: Mapping[str, Any],
    syllables_per_second: float = DEFAULT_SYLLABLES_PER_SECOND,
) -> None:
    """Validate dialogue length before the script is passed to later tasks."""
    if syllables_per_second <= 0:
        raise ValueError("syllables_per_second는 0보다 커야 합니다.")

    scenes = document.get("scenes") or []
    for index, scene in enumerate(scenes, start=1):
        auditory = scene.get("auditory") or {}
        voiceover = auditory.get("voiceover")
        if not isinstance(voiceover, str) or not voiceover.strip():
            continue
        time_range = scene["time_range_sec"]
        start = time_range["start"]
        end = time_range["end"]
        max_syllables = max(1, int((end - start) * syllables_per_second))
        actual_syllables = count_speech_syllables(voiceover)
        if actual_syllables > max_syllables:
            raise ScriptDialogueLengthError(
                scene_number=index,
                max_syllables=max_syllables,
                actual_syllables=actual_syllables,
            )


def _is_retryable_provider_error(error: OpenRouterRequestError) -> bool:
    """Retry only provider availability failures, not invalid requests."""
    message = str(error).lower()
    if error.status_code in (429, 503):
        return True
    if error.status_code == 404:
        return "no endpoints available" in message
    if error.status_code == 400:
        return "provider returned error" in message
    return False


class OpenRouterClient:
    """Small dependency-injectable client for OpenRouter script generation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_model: str | None = None,
        api_url: str = DEFAULT_API_URL,
        timeout_seconds: int = 60,
        # Keep five total attempts when no database-backed settings are configured.
        max_attempts: int = 5,
        opener: Callable[..., Any] = urlopen,
        retry_delay_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts는 1 이상이어야 합니다.")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds는 0 이상이어야 합니다.")
        self.api_key = api_key
        self.model = model
        self.fallback_model = fallback_model
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.opener = opener
        self.retry_delay_seconds = retry_delay_seconds
        self.sleep = sleep

    @classmethod
    def from_env(cls) -> "OpenRouterClient":
        from app.core.config import settings as app_settings

        model = os.getenv("OPENROUTER_SCRIPT_MODEL") or DEFAULT_MODEL
        return cls(
            api_key=(
                os.getenv("OPENROUTER_SCRIPT_API_KEY")
                or os.getenv("OPENROUTER_API_KEY")
                or app_settings.OPENROUTER_API_KEY
                or ""
            ),
            model=model,
            fallback_model=os.getenv("OPENROUTER_FALLBACK_MODEL") or model,
            api_url=os.getenv("OPENROUTER_API_URL") or DEFAULT_API_URL,
        )

    def generate_script(
        self,
        request: ScriptGenerationRequest,
        *,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise OpenRouterConfigurationError(
                "OPENROUTER_SCRIPT_API_KEY 또는 OPENROUTER_API_KEY가 설정되지 않았습니다."
            )
        if not self.model:
            raise OpenRouterConfigurationError("OPENROUTER_SCRIPT_MODEL이 설정되지 않았습니다.")

        attempt_limit = self.max_attempts if max_attempts is None else max_attempts
        if attempt_limit < 1:
            raise ValueError("max_attempts는 1 이상이어야 합니다.")
        last_error: OpenRouterError | None = None
        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)

        for attempt in range(attempt_limit):
            # 재시도에서도 Colab과 동일한 prompt를 유지한다.
            model = models[min(attempt, len(models) - 1)]
            try:
                return self._generate_once(request, model)
            except ScriptValidationError as error:
                last_error = error
            except OpenRouterRequestError as error:
                if not _is_retryable_provider_error(error):
                    raise
                last_error = error

            if attempt < attempt_limit - 1:
                self.sleep(self.retry_delay_seconds)

        assert last_error is not None
        raise last_error
    def _generate_once(
        self,
        request: ScriptGenerationRequest,
        model: str,
        attempt: int = 0,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 2000,
            "reasoning": {"exclude": True},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "reels_script",
                    "strict": True,
                    "schema": SCRIPT_RESPONSE_SCHEMA,
                },
            },
            "messages": [{
                "role": "user",
                "content": build_script_message_content(
                    request,
                    build_script_prompt(request),
                ),
            }],
        }
        http_request = Request(
            self.api_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        content = http_request.data or b""
        image_included = request.image_url is not None
        logger.info(
            "script generation request: model=%s endpoint=%s attempt=%d "
            "image_included=%s image_count=%d payload_bytes=%d",
            model,
            self.api_url,
            attempt + 1,
            image_included,
            1 if image_included else 0,
            len(content),
        )

        try:
            with self.opener(http_request, timeout=self.timeout_seconds) as response:
                response_body = json.loads(response.read().decode("utf-8"))
                logger.info(
                    "script generation response: model=%s attempt=%d status=%s "
                    "provider=%s response_id=%s",
                    model,
                    attempt + 1,
                    getattr(response, "status", "unknown"),
                    response_body.get("provider"),
                    response_body.get("id"),
                )
        except HTTPError as error:
            detail = _http_error_detail(error)
            suffix = f": {detail}" if detail else ""
            request_id = _http_error_request_id(error)
            logger.warning(
                "script generation provider error: model=%s endpoint=%s "
                "attempt=%d status=%s image_included=%s request_id=%s detail=%s",
                model,
                self.api_url,
                attempt + 1,
                error.code,
                image_included,
                request_id or "unknown",
                detail or "unknown",
            )
            raise OpenRouterRequestError(
                f"OpenRouter 요청이 거부되었습니다. HTTP {error.code}{suffix}",
                status_code=error.code,
            ) from error
        except URLError as error:
            raise OpenRouterRequestError("OpenRouter에 연결하지 못했습니다.") from error
        except (JSONDecodeError, UnicodeDecodeError) as error:
            raise OpenRouterRequestError("OpenRouter 응답을 JSON으로 읽지 못했습니다.") from error

        try:
            content = response_body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ScriptValidationError("OpenRouter 응답에 choices.message.content가 없습니다.") from error

        normalized = normalize_script_subtitles(extract_script_json(content))
        if request.template_scene_plan:
            try:
                normalized = normalize_generated_script_to_plan(
                    normalized,
                    request.template_scene_plan,
                )
            except GenerationTemplateError as error:
                raise ScriptValidationError(str(error)) from error
        structurally_valid = validate_script_document(
            normalized,
            max_duration_seconds=request.max_duration_seconds,
            validate_dialogue=False,
        )
        fitted = fit_script_dialogue_lengths(structurally_valid)
        return validate_script_document(
            fitted,
            max_duration_seconds=request.max_duration_seconds,
        )
