"""Generate structured short-form scripts through the OpenRouter chat API."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from json import JSONDecodeError
from copy import deepcopy
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b:free"
DEFAULT_FALLBACK_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
DEFAULT_SYLLABLES_PER_SECOND = 4.5
MAX_SCRIPT_DURATION_SECONDS = 30
STRICT_SCHEMA_ENV = "OPENROUTER_SCRIPT_USE_RESPONSE_FORMAT"


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
                "usp": {"type": "string"},
            },
        },
        "customer": {
            "type": "object",
            "additionalProperties": False,
            "required": ["main_target", "pain_point"],
            "properties": {
                "main_target": {"type": ["string", "null"]},
                "pain_point": {"type": ["string", "null"]},
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
                "goal": {"type": ["string", "null"]},
                "cta_action": {"type": "string"},
                "channel_platform": {"type": "string"},
                "main_target": {"type": ["string", "null"]},
                "ad_planner": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["persona"],
                    "properties": {"persona": {"type": ["string", "null"]}},
                },
                "speaker": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["persona", "tone"],
                    "properties": {
                        "persona": {"type": ["string", "null"]},
                        "tone": {"type": ["string", "null"]},
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
                "video_duration": {"type": "string"},
                "required_scenes_elements": {"type": ["string", "null"]},
                "forbidden_scenes_elements": {"type": ["string", "null"]},
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
                    "section": {"type": "string"},
                    "time_range_sec": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start", "end"],
                        "properties": {
                            "start": {"type": "number", "minimum": 0},
                            "end": {"type": "number", "exclusiveMinimum": 0},
                        },
                    },
                    "visual": {"type": "string"},
                    "auditory": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["subtitle", "voiceover"],
                        "properties": {
                            "subtitle": {"type": ["string", "null"]},
                            "voiceover": {"type": ["string", "null"]},
                        },
                    },
                    "intent": {"type": "string"},
                    "notes": {"type": ["string", "null"]},
                },
            },
        },
        "etc": {
            "type": "object",
            "additionalProperties": False,
            "required": ["additional_information", "video_ads_methodology"],
            "properties": {
                "additional_information": {"type": ["string", "null"]},
                "video_ads_methodology": {"type": ["string", "null"]},
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


def build_script_prompt(request: ScriptGenerationRequest) -> str:
    """Build a constrained prompt from product data supplied by the caller."""
    usp = request.product.get("usp")
    has_usp = usp is not None and (not isinstance(usp, str) or bool(usp.strip()))
    product_json = json.dumps(
        prepare_product_for_prompt(request.product),
        ensure_ascii=False,
        indent=2,
    )
    reviews_json = json.dumps(request.reviews or [], ensure_ascii=False, indent=2)
    custom_prompt = request.custom_prompt.strip() if request.custom_prompt else ""
    custom_instruction = (
        "- 아래 추가 프롬프트의 지시도 반영하세요.\n"
        f"추가 프롬프트: {custom_prompt}"
        if custom_prompt
        else ""
    )
    return f"""당신은 공동구매 광고 숏폼 스크립트 작성자입니다.

아래 상품 데이터에 실제로 포함된 정보만 사용해 {request.channel}용 스크립트를 작성하세요.
확인되지 않은 효능, 인증, 소재, 가격, 할인, 사용 후기 또는 제품 특징을 추측해서 추가하지 마세요.
상품 데이터에 없는 정보는 빈 문자열 또는 빈 배열로 두세요.

필수 조건:
- 세로형 9:16 영상
- 최대 {request.max_duration_seconds}초
- 선택한 광고 방법론을 적용하고 마지막 장면에 CTA를 포함
- 사용할 수 있는 광고 방법론: Hook-Body-CTA, PAS, AIDA, BAB(Before-After-Bridge), 4Ps(Promise-Picture-Proof-Push)
- Anti-Slop Prompt For Video를 선택하는 경우 과도하게 인공적인 표현을 줄이고 제품 사용 흔적(signs of use)을 자연스럽게 반영하세요.
- 장면마다 하나의 핵심 행동
- 영상 없이 자막만 읽어도 이해 가능
- 첫 장면은 시선을 끌고, 마지막 장면은 구체적인 CTA를 포함
- 자막은 짧게 작성하고 화면에 넣을 문구와 내레이션을 구분
- 대사는 장면 시간 안에 읽을 수 있도록 작성하고, 평균 1초당 4.5음절을 기준으로 계산
- 각 장면의 대사 음절 수가 해당 장면 시간 x 4.5를 넘지 않도록 작성
- 추상적인 표현 대신 dolly, pan, tilt, crane, push-in, rack focus, locked-off 같은 카메라 용어와 Reduce fill, Cool down, Desaturate, Diffuse, Dim down, Reposition 같은 조명 용어를 사용하세요.
- 등장인물이 카메라를 주시하며 말하지 않는다.
- 같은 인물의 얼굴, 헤어스타일, 의상이 장면마다 유지되도록 한다.
- 상품 이미지의 형태, 색상, 라벨, 용기가 바뀌지 않도록 한다.
- 영상 내에 포함해야 하는 유일한 텍스트는 상품에 표기된 텍스트이며, 상품 이외의 물체에는 텍스트를 넣지 마세요.
- 각 장면의 visual은 100자 미만으로 작성하세요.
- 상품 라벨의 글자와 로고는 식별 가능한 정면 클로즈업으로 보여주지 않는다.
- 상품 라벨은 화면 바깥으로 일부 잘리거나 손·소품·그림자에 의해 부분적으로 가려지게 하세요.
- 자막, 가격, 할인율, CTA 문구는 영상에 삽입하지 않는다.
- 유저가 프롬프트를 통해 해당 상품정보를 입력해주었다면 해당 내용을 반영하세요.
{custom_instruction}
{"- 최종 장면 종료 시간은 다음 중 하나로 작성: " + ", ".join(str(value) for value in request.supported_video_durations) if request.supported_video_durations else ""}
{"- USP가 비어있거나 null인 경우, 상품 데이터의 다른 정보만을 바탕으로 USP를 도출해 product.usp에 출력하고 스크립트에 반영하세요." if not has_usp else "- USP가 입력되어 있으면 입력받은 USP값을 그대로 출력하고 product.usp에 저장한 뒤 스크립트에 반영하세요."}

다음 JSON 객체만 반환하세요. Markdown 코드블록이나 설명은 붙이지 마세요.
{{
  "meta": {{
    "output_format_version": "1.0",
    "language": "ko"
  }},
  "product": {{
    "usp": "상품 데이터로 확인되거나 도출한 USP"
  }},
  "customer": {{
    "main_target": "주요 타깃",
    "pain_point": "타깃의 고민"
  }},
  "ads": {{
    "goal": "광고 목표",
    "cta_action": "시청자가 할 행동",
    "channel_platform": "업로드 채널",
    "ad_planner": {{"persona": null}},
    "speaker": {{"persona": null, "tone": null}},
    "main_target": "주요 타깃"
  }},
  "video": {{
    "video_duration": "{request.max_duration_seconds}",
    "required_scenes_elements": null,
    "forbidden_scenes_elements": null
  }},
  "scenes": [
    {{
      "section": "Hook",
      "time_range_sec": {{"start": 0, "end": 3}},
      "visual": "화면에 보일 장면과 행동",
      "auditory": {{
        "subtitle": "화면 자막",
        "voiceover": "내레이션"
      }},
      "intent": "장면의 연출 의도",
      "notes": "연출 의도"
    }}
  ],
  "etc": {{
    "additional_information": null,
    "video_ads_methodology": "선택한 광고 방법론"
  }}
}}

타깃: {request.target_audience}
상품 데이터:
{product_json}
추가 리뷰 데이터:
{reviews_json}
"""


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
    document: Mapping[str, Any], max_duration_seconds: int | None = None
) -> dict[str, Any]:
    """Validate the minimum contract consumed by later video tasks."""
    if not isinstance(document, Mapping):
        raise ScriptValidationError("스크립트 응답은 JSON 객체여야 합니다.")

    # Keep previously generated documents readable while new API responses use
    # the current PRD schema below.
    if "product" not in document:
        return _validate_legacy_script_document(document, max_duration_seconds)

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

    validate_dialogue_lengths(document)

    return dict(document)


def _validate_legacy_script_document(
    document: Mapping[str, Any], max_duration_seconds: int | None = None
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

    validate_dialogue_lengths(document)
    return dict(document)


def count_speech_syllables(text: str) -> int:
    """Count spoken characters while ignoring whitespace and punctuation."""
    return sum(1 for character in text if character.isalnum())


def normalize_script_subtitles(document: Mapping[str, Any]) -> dict[str, Any]:
    """Convert escaped line breaks in generated subtitles into actual line breaks."""
    normalized = deepcopy(document)
    for scene in normalized.get("scenes") or []:
        auditory = scene.get("auditory") or {}
        subtitle = auditory.get("subtitle")
        if isinstance(subtitle, str):
            auditory["subtitle"] = subtitle.replace("\\r\\n", "\n").replace("\\n", "\n")
    return normalized


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
        fallback_model: str | None = DEFAULT_FALLBACK_MODEL,
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
        return cls(
            api_key=os.getenv("OPENROUTER_SCRIPT_API_KEY", ""),
            model=os.getenv("OPENROUTER_SCRIPT_MODEL") or DEFAULT_MODEL,
            fallback_model=os.getenv("OPENROUTER_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL,
            api_url=os.getenv("OPENROUTER_API_URL") or DEFAULT_API_URL,
        )

    def generate_script(self, request: ScriptGenerationRequest) -> dict[str, Any]:
        if not self.api_key:
            raise OpenRouterConfigurationError(
                "OPENROUTER_SCRIPT_API_KEY가 설정되지 않았습니다."
            )
        if not self.model:
            raise OpenRouterConfigurationError("OPENROUTER_SCRIPT_MODEL이 설정되지 않았습니다.")

        last_error: OpenRouterError | None = None
        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)

        for attempt in range(self.max_attempts):
            # Fallback 모델을 사용한 뒤에도 설정된 재실행 횟수까지 계속 검증한다.
            model = models[min(attempt, len(models) - 1)]
            try:
                return self._generate_once(request, model, attempt, last_error)
            except ScriptValidationError as error:
                last_error = error
            except OpenRouterRequestError as error:
                if not _is_retryable_provider_error(error):
                    raise
                last_error = error

            if attempt < self.max_attempts - 1:
                self.sleep(self.retry_delay_seconds)

        assert last_error is not None
        raise last_error
    def _generate_once(
        self,
        request: ScriptGenerationRequest,
        model: str,
        attempt: int,
        previous_error: OpenRouterError | None = None,
    ) -> dict[str, Any]:
        retry_instruction = ""
        if attempt > 0:
            retry_instruction = f"""

이전 스크립트 생성 결과가 검증에 실패했습니다.
실패 사유: {previous_error}
상품 정보와 기존 광고 방법론·장면 의도는 유지하되, 실패 사유를 해결한 전체 스크립트를 다시 생성하세요.
영상은 9:16 세로형 조건을 따르되 설정값을 meta에 추가하지 말고,
상품 데이터에 없는 장면이나 효능을 만들지 마세요. JSON만 반환하세요.
"""

        payload = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 2000,
            "reasoning": {"exclude": True},
            "messages": [{
                "role": "user",
                "content": build_script_message_content(
                    request,
                    build_script_prompt(request) + retry_instruction,
                ),
            }],
        }
        # Keep Structured Output enabled by default. This switch is only for
        # diagnosing providers that reject response_format routing.
        if os.getenv(STRICT_SCHEMA_ENV, "true").strip().lower() not in {
            "false",
            "0",
            "no",
        }:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "reels_script",
                    "strict": True,
                    "schema": SCRIPT_RESPONSE_SCHEMA,
                },
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

        try:
            with self.opener(http_request, timeout=self.timeout_seconds) as response:
                response_body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = _http_error_detail(error)
            suffix = f": {detail}" if detail else ""
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

        return validate_script_document(
            normalize_script_subtitles(extract_script_json(content)),
            max_duration_seconds=request.max_duration_seconds,
        )
