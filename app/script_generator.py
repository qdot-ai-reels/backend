"""Generate structured short-form scripts through the OpenRouter chat API."""

from __future__ import annotations

import json
import logging
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
DEFAULT_SYLLABLES_PER_SECOND = 4.5
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
            "\t- USP(Unique Selling Point)값이 null이면",
            "\t\t- 상품정보 항목의 내용에 근거하여 USP(Unique Selling Point)를 추론하여 작성하여 출력할 것",
            "\t- USP(Unique Selling Point)값이 null이 아니면",
            "\t\t- 입력한 그대로 출력할 것",
            f"- Curator Pitch: {_format_product_prompt_value(curator_pitch)}",
            f"- Hashtags: {_format_product_prompt_value(hashtags)}",
            f"- Description Text: {_format_product_prompt_value(description_text)}",
            f"- Detail Info: {_format_product_prompt_value(detail_info)}",
            f"- Reviews: {_format_product_prompt_value(product_reviews)}",
        ]
    )


def extract_cta_action(custom_prompt: str) -> str:
    """Extract the CTA value from the team's `CTA: ...` input convention."""
    for line in custom_prompt.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().lower() in {"cta", "cta action"}:
            return value.strip() or "null"
    return "null"


def build_script_prompt(request: ScriptGenerationRequest) -> str:
    """Build a constrained prompt from product data supplied by the caller."""
    product_prompt_fields = build_product_prompt_fields(request.product, request.reviews)
    custom_prompt = request.custom_prompt.strip() if request.custom_prompt else ""
    cta_action = extract_cta_action(custom_prompt)
    return f"""
당신은 공동구매 광고 숏폼 스크립트 작성자입니다.

아래 상품 데이터에 실제로 포함된 정보만 사용해 스크립트를 작성하세요.

### Condition
#### 1. 광고 진실성
(1) 입력된 상품 정보 안에서만 사실을 작성한다.
(2) 과대광고성 문구(효능 과장, 근거 없는 내용 등)를 포함하지 말아야 한다.
(3) 지나치게 과장하지 말아야 한다.
(4) 실제 사용자의 사용담 처럼 허위 경험이 들어가면 안된다.

#### 2. 상품 정보
(1) 비어있는 상품 정보들 중에, 유저가 프롬프트를 통해 해당 상품정보를 입력해주었다면, 이를 반영하여 비어 있는 상품 정보를 채워넣어라.
(2) 입력된 상품 정보들 중에서 usp 값이 비어있으며 유저가 USP(Unique Selling Point)에 대한 정보를 제공하지 않았다면, 다른 상품정보 항목의 내용에 근거하여 USP(Unique Selling Point)를 추론하여 작성하라.

#### 3. 핵심 원칙
(1) 숏폼에서는 첫 1~3초 안에 계속 볼지 넘길지가 결정되기 때문에, 소비자의 문제나 관심사를 바로 건들여야 한다.
(2) 이 상품이 어떤 상황에서 왜 좋은지를 보여주어야 한다.
(3) 상품의 기능, 사용 장면처럼 소비자가 판단할 수 있는 정보가 들어가야 한다.

#### 4. 영상 구현 구체성
(1) 추상적 설명 대신 구체적 지시
- 'Masterpiece', 'Hyper-realistic', 'Stunning', 'Cinematic'와 같은 추상적 표현 대신 카메라 용어, 조명 언어를 작성한다.
  - 카메라 용어 예시: dolly, pan, tilt, crane, push-in, rack focus, locked-off
  - 조명 용어 예시: Reduce fill, Cool down, Desaturate, Diffuse, Dim down, Reposition
(2) 세부 규칙
- 등장인물이 카메라를 주시하며 말하지 않는다.
- 같은 인물의 얼굴, 헤어스타일, 의상이 장면마다 유지되도록 한다.
- 상품 이미지의 형태, 색상, 라벨, 용기가 바뀌지 않도록 한다.
- 영상 생성 모델이 만드는 영상 프레임에는 상품에 표기된 텍스트 외의 글자를 직접 넣지 않는다.

#### 5.  영상 내 상품 텍스트 노출 최소화
- 상품 라벨의 글자와 로고는 식별 가능한 정면 클로즈업으로 보여주지 않는다.
- 상품의 형태, 색상, 용기 구조는 유지하되 라벨은 비가독 상태로 표현한다.
- 상품 라벨은 화면 바깥으로 일부 잘리거나, 손·소품·그림자에 의해 부분적으로 가려져야 한다.
- 영상 생성 모델이 만드는 영상 프레임 안에는 자막, 가격, 할인율, CTA 문구를 직접 삽입하지 않는다.

#### 6. HyperFrames 캡션
- `auditory.subtitle`은 영상 생성 모델이 그리는 글자가 아니라, 영상 생성 후 HyperFrames가 별도로 추가하는 텍스트 애니메이션용 캡션이다.
- `auditory.subtitle`은 `voiceover`와 구분하여 작성하고, 캡션으로 표시할 짧은 문구를 넣는다.
- 캡션을 표시할 수 있도록 `auditory.subtitle`을 `null`이나 빈 문자열로 반환하지 않는다.
- 영상 생성 모델의 `visual` 설명에는 자막·가격·할인율·CTA 문구를 넣지 않는다. 해당 문구는 `auditory.subtitle`로만 전달한다.

#### 7. 기타
(1) 영상 스크립트 내의 음성 대사는 1초에 4.5음절이 넘지 않도록 한다.
각 장면의 대사 음절 수가 해당 장면 시간 × 4.5를 넘지 않도록 작성하세요.
대사는 장면 시간 안에 읽을 수 있도록 짧게 작성하세요.
- 각 장면의 허용 음절 수는 장면 시간(초) × 4.5를 계산한 뒤 소수점 이하는 버린다.
- 예를 들어 장면 시간이 2초이면 최대 9음절, 1.5초이면 최대 6음절이다.
- 허용 음절 수를 단 1개라도 초과하는 voiceover는 작성하지 않는다.
- 대사를 작성한 뒤 각 장면의 voiceover 음절 수를 직접 확인하고, 제한을 초과하면 더 짧게 다시 작성한다.
- 장면 시간이 짧은 경우 한두 단어 수준으로 간결하게 작성한다.

### Methodology

#### 필수 방법론
- 영상 마지막 부분에 CTA(Call To Action)을 추가
\t- 'scenes' 부분의 가장 마지막 Section에는 유저가 해당 광고를 보고 특정한 액션을 취할 수 있어야 한다.

#### 선택 방법론
- Hook-Body-CTA
- PAS
- AIDA
- BAB(Before-After-Bridge)
- 4Ps(Promise-Picture-Proof-Push)
- Anti-Slop Prompt For Video: 현실성 있는 영상을 위해 불완전성(imperfection)을 더하라
\t- Product
\t\t- signs of use(제품 사용 흔적)
\t- Camera
\t\t- slight handheld motion(약간의 핸드헬드 움직임)
\t- People
\t\t- imperfect skin texture(고르지 않은 피부결)
\t\t- subtle blemishes(미세한 잡티)
\t\t- wrinkled fabric(주름진 옷감)
\t\t- natural and subtle asymmetry(자연스럽고 미세한 비대칭)


### 요구사항
- CTA Action: {cta_action}
- Video duration: {request.max_duration_seconds}
- Upload Channel: {request.channel}

### 상품 정보
{product_prompt_fields}
{f"\n\n{request.retry_instruction.strip()}" if request.retry_instruction and request.retry_instruction.strip() else ""}
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
        model = os.getenv("OPENROUTER_SCRIPT_MODEL") or DEFAULT_MODEL
        return cls(
            api_key=os.getenv("OPENROUTER_SCRIPT_API_KEY", ""),
            model=model,
            fallback_model=os.getenv("OPENROUTER_FALLBACK_MODEL") or model,
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

            if attempt < self.max_attempts - 1:
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

        return validate_script_document(
            normalize_script_subtitles(extract_script_json(content)),
            max_duration_seconds=request.max_duration_seconds,
        )
