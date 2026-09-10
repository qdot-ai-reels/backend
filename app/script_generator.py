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
DEFAULT_MODEL = "google/gemini-3.8-flash"
# Keep the dialogue limit aligned with the latest Colab prompt.
DEFAULT_SYLLABLES_PER_SECOND = 3.5
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
                    "description": "해당 상품의 Unique Selling Point. USP 값을 입력받은 경우 입력받은 USP값을 그대로 출력해야 하며, 입력 받지 못한 경우 상품정보 항목의 내용에 근거하여 USP를 추론하여 작성하여 출력하여야 함.",
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
                    "description": "해당 상품의 메인 타겟 고객. 없으면 null",
                },
                "pain_point": {
                    "type": ["string", "null"],
                    "description": "해당 상품의 주요 타겟 고객이 가진 핵심 문제점 또는 불편함. 없으면 null.",
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
                "goal": {"type": ["string", "null"], "description": "해당 광고의 Goal. 없으면 null"},
                "cta_action": {"type": "string", "description": "해당 광고의 CTA Action"},
                "channel_platform": {"type": "string", "description": "해당 광고가 업로드되는 채널"},
                "main_target": {"type": ["string", "null"], "description": "이번 광고에서 실제로 공략할 Target. 없으면 null"},
                "ad_planner": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["persona"],
                    "properties": {
                        "persona": {
                            "type": ["string", "null"],
                            "description": "광고 영상 기획자의 특성을 의미합니다. 직책, 성격, 특성, 가치관, 과거 경력 등등. 없으면 null.",
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
                            "description": "화자의 특성을 의미합니다. 성격, 특성, 가치관, 과거 경력 등등. 외형 묘사 절대 금지. 없으면 null",
                        },
                        "tone": {
                            "type": ["string", "null"],
                            "description": "해당 광고 영상에 어울리는 화자의 말투. 없으면 null",
                        },
                    },
                },
            },
        },
        "video": {
            "type": "object",
            "additionalProperties": False,
            "required": ["video_duration"],
            "properties": {
                "video_duration": {"type": "number", "minimum": 1, "description": "생성하는 광고 영상의 길이(초)"},
            },
        },
        "scenes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "section",
                    "time_range_sec",
                    "visual",
                    "auditory",
                    "intent",
                    "exclusion_list_about_physical_motions",
                    "notes",
                ],
                "properties": {
                    "section": {"type": "string", "description": "영상의 부분 파트"},
                    "time_range_sec": {
                        "type": "object",
                        "additionalProperties": False,
                        "description": "해당 장면이 재생되는 시작 및 종료 시간 범위(초)",
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
                        "description": "영상 생성 모델에 입력할 시각 요소 설명. 피사체의 상태, 행동, 물리적 상호작용, 결과, 카메라 움직임을 구체적으로 작성한다. 300자 이내, 매우 구체적으로 작성. '사용할 촬영/편집 기법' 사용 시 해당 기법 명칭을 정확히 기입할 것. 불필요한 수식어는 제거",
                    },
                    "auditory": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["subtitle", "voiceover"],
                        "properties": {
                            "subtitle": {
                                "type": ["string", "null"],
                                "description": "영상의 부분 파트의 Caption(텍스트 애니메이션).",
                            },
                            "voiceover": {
                                "type": ["string", "null"],
                                "description": "영상의 부분 파트의 목소리 추가. 1초에 3.5음절 미만.",
                            },
                        },
                    },
                    "intent": {
                        "type": "string",
                        "description": "영상의 부분 파트의 연출 의도 설명",
                    },
                    "exclusion_list_about_physical_motions": {
                        "type": "string",
                        "description": "해당 장면에서 포함되면 안되는 물리적 동작들. 'Physical-Safe Motion & Continuity', 'Physical-Safe Scene Selection'의 항목을 적극적으로 참고하여 작성할 것. 다양하게 작성. 구체적으로 작성할 것.",
                    },
                    "notes": {
                        "type": ["string", "null"],
                        "description": "영상의 부분 파트의 기타 추가설명. 없으면 null",
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
                    "description": "유저가 입력한 다양한 추가사항. 없으면 null",
                },
                "video_ads_methodology": {
                    "type": ["string", "null"],
                    "description": "해당 광고 영상에 활용할 다양한 방법론. 없으면 null",
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
    use_default_prompt: bool = True

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
    """Build the latest Colab script prompt with runtime values inserted."""
    product_prompt_fields = build_product_prompt_fields(request.product, request.reviews)
    custom_prompt = request.custom_prompt.strip() if request.custom_prompt else ""
    cta_action = extract_cta_action(custom_prompt)
    if not request.use_default_prompt:
        return f"""
당신은 공동구매 광고 숏폼 스크립트 작성자입니다.

### 사용자 지정 프롬프트
{custom_prompt}

### 요구사항
- CTA Action: {cta_action}
- Video duration: {request.max_duration_seconds}
- Upload Channel: {request.channel}
- 상품 정보에 없는 사실이나 과장 표현을 만들지 마세요.
- 출력은 기존 Structured Output JSON 형식을 따르고 하나 이상의 scenes를 포함하세요.

### 상품 정보
{product_prompt_fields}
{f"\n\n{request.retry_instruction.strip()}" if request.retry_instruction and request.retry_instruction.strip() else ""}
"""


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
(2) USP가 비어 있으면 다른 입력 상품정보에서 확인 가능한 차별점을 바탕으로 요약하여 작성한다.
(3) 입력 정보에서 확인할 수 없는 차별점은 USP로 추론하지 않는다.

#### 3. 핵심 원칙
(1) [최우선 원칙] 물리적 움직임을 사실적으로 생성하는 것보다, 물리적 움직임을 생성하지 않아도 광고 메시지를 전달할 수 있는 촬영 및 편집 방식을 우선한다.
(2) 숏폼에서는 첫 1~3초 안에 계속 볼지 넘길지가 결정되기 때문에, 소비자의 문제나 관심사를 시각적으로 바로 건들여야 한다.
(3) 이 상품이 어떤 상황에서 왜 좋은지를 보여주어야 한다.
(4) 상품의 기능, 사용 장면처럼 소비자가 판단할 수 있는 시각적으로 정보가 들어가야 한다.
  - 단, 'Physical-Safe Scene Selection'의 조건을 만족하여야 한다. 자세한 내용은 아래에 확인.
(5) 등장인물이 행동해야 한다.

#### 4. 영상 구현 구체성
(1) 추상적 설명 대신 구체적 지시
- 'Masterpiece', 'Hyper-realistic', 'Stunning', 'Cinematic'과 같은 추상적 표현 대신 구체적인 카메라/조명 용어를 사용한다.
- 카메라 예시: locked-off, push-in, dolly, pan, tilt, rack focus
- 조명 예시: Reduce fill, Cool down, Desaturate, Diffuse, Dim down, Reposition

(2) 세부 규칙
- 등장인물이 카메라를 주시하며 말하지 않는다.
- 동일 인물의 얼굴, 헤어스타일, 의상을 장면마다 유지한다.
- 상품의 형태, 색상, 라벨, 용기가 변하지 않도록 한다.
- 상품에 실제 표기된 텍스트 외의 텍스트를 추가하지 않는다.
- 상품 라벨은 식별 가능한 정면 클로즈업을 피한다.

(3) Physical-Safe Motion & Continuity
- [필수] 광고 상품의 물리적 움직임을 생성하는 것은 극도로 자제하고, (아래의)'사용할 촬영/편집 기법'을 매우 적극적으로 활용하여 광고 메시지를 전달하는 방식을 우선한다.
- 광고 상품의 물리적 움직임이 필요한 경우 아래의 규칙을 따른다
  - 물리적으로 복잡한 장면보다 동일한 광고 메시지를 전달하는 더 단순한 장면을 선택한다.
  - 액체, 불, 연기, 거품, 끈, 케이블 등 복잡한 물리 움직임은 동일한 메시지를 전달할 수 있다면 정지 상태 또는 컷 전환으로 대체한다.
  - 하나의 Section에는 하나의 핵심 물리적 행동만 사용하며, 동시에 여러 주요 물체를 움직이지 않는다.
  - 행동이 필요한 경우 하나의 방향과 단순한 인과관계로 제한하고, 불필요한 회전·충돌·반동·변형을 피한다.
  - 물체는 중력과 지지면의 영향을 받으며, 손이나 다른 물체의 명확한 상호작용 없이 위치나 방향이 바뀌지 않는다.
  - 물체의 생성·소멸·복제·순간이동·관통을 금지한다.
  - 각 Section은 직전 Section의 주요 물체 위치, 방향, 상태 및 접촉 관계를 유지한다. 상태가 변경되면 행동 또는 명시적인 컷 전환으로 설명한다.
  - 새로운 물체는 손에 들고 들어오거나 화면 밖에서 가져오는 등 등장 원인을 명확히 한다.
  - 카메라 움직임과 피사체/물체 움직임은 별도로 작성한다.

(4) [매우 중요] 사용할 촬영/편집 기법: 광고 상품의 물리적 움직임을 생성하는 것은 극도로 자제하는 대신 다음과 같은 기법을 통해 상품을 소개하라
- Freeze Frame
  - 기법 설명: 특정 순간의 한 프레임을 그대로 멈춰 피사체와 배경의 움직임을 완전히 차단하는 기법. 정지된 화면 위에 카피나 그래픽을 추가하기 좋음
- Pose-to-Pose + Jump Cut
  - 기법 설명: 연속적인 움직임을 생성하지 않고 핵심적인 포즈만 여러 개 만든 뒤 중간 동작을 컷으로 생략하는 방식. `Pose A → Cut → Pose B → Cut → Pose C` 구조
- Whip Pan Transition
  - 기법 설명: 카메라를 매우 빠르게 좌우 또는 상하로 움직여 강한 모션 블러를 만든 뒤, 블러가 발생한 순간 다른 장면으로 전환하는 기법
- Object Occlusion Cut
  - 기법 설명: 사람이나 물체가 카메라 앞을 지나가며 화면 전체를 가리는 순간 컷을 넣어 새로운 장면으로 연결하는 방식. 가려진 동안 장면의 물리적 연속성을 끊을 수 있음
- Still Image + Camera Move
  - 기법 설명: 피사체 자체는 정지시킨 채 카메라의 Zoom, Pan, Dolly, Orbit 등의 움직임만 주는 방식. 사진에 생명력을 불어넣는 듯한 효과
- Bullet Time
  - 기법 설명: 피사체의 특정 순간을 완전히 또는 거의 정지시키고 카메라가 피사체 주변을 이동하는 듯한 연출. 공중 점프나 액션 장면에 특히 효과적
- Cinemagraph
  - 기법 설명: 전체 화면은 정지된 사진처럼 유지하면서 머리카락, 연기, 물, 빛 등 일부 요소만 움직이게 하는 기법. '살아있는 사진' 느낌을 줌
- Speed Ramp → Freeze
  - 기법 설명: 정상 속도 또는 빠른 움직임에서 점차 Slow Motion으로 전환한 후 특정 순간에 완전히 Freeze하는 방식. 중요한 순간을 강조하기 좋음
- Match Cut
  - 기법 설명: 움직임을 직접 연결하지 않고 형태, 색상, 구도, 크기 등이 비슷한 두 장면을 이어 붙이는 기법. 서로 전혀 다른 공간도 자연스럽게 연결 가능

(5) Visual 작성 구조
- Visual은 가능한 한 다음 순서로 작성한다.
  1. 피사체와 물체의 현재 상태
  2. 피사체의 행동
  3. 물리적 상호작용 및 결과
  4. 카메라 움직임
- 카메라 움직임은 피사체의 행동과 별도의 문장으로 작성한다.
- 카메라 움직임이 피사체나 물체의 움직임을 유발하는 것처럼 표현하지 않는다

(6) Physical-Safe Scene Selection: 광고 장면에서 '물체의 움직임이 필요하다면', 광고적으로 좋은 장면과 영상 생성 모델이 물리적으로 안정적으로 구현할 수 있는 장면의 교집합을 선택한다.

a. Scene Selection Principle
- 광고적으로 인상적인 행동이라도 물리적 구현 위험이 높다면 해당 행동을 선택하지 않는다.
- 동일한 광고 메시지를 전달할 수 있다면, 더 단순하고 안정적인 물리적 행동을 우선 선택한다.
- "더 화려한 행동"보다 "더 안정적으로 생성되는 행동"을 우선한다.
- 물리적으로 복잡한 행동을 억지로 구현하지 말고, 동일한 의미를 전달하는 단순한 행동으로 대체한다.
- 장면을 생성한 후 물리적 문제를 수정하는 것보다, 처음 장면을 선택하는 단계에서 위험한 행동을 제외한다.

b. Physical Risk Priority: 다음 위험은 장면 선택 단계에서 우선적으로 제거한다.
- P0: 반드시 피한다.
  - 순간이동
  - 갑작스러운 생성/소멸/복제
  - 물체/신체 관통
  - 접촉 없는 물체 이동
  - 원인 없는 위치/방향 변화
  - Section 간 상태 불일치

- P1: 가능한 한 피한다.
  - 갑작스러운 속도/방향 변화
  - 중력에 반하는 움직임
  - 공중에 떠 있는 물체
  - 비현실적인 손/팔/관절 움직임
  - 발 미끄러짐
  - 옷/머리카락 관통
  - 급격한 변형

- P2: 필요하지 않으면 피한다.
  - 여러 물체의 동시 이동
  - 복잡한 액체/거품/연기/불
  - 복잡한 충돌/반동
  - 줄/케이블 얽힘
  - 넘어짐/튕김/파손
  - 복잡한 균형

(7) Camera-Subject Motion Separation
- 카메라 움직임과 피사체/물체 움직임은 별도로 작성한다.
- Camera move가 피사체나 물체의 물리적 이동을 의미하지 않도록 한다.
- 예: "제품은 테이블 위에서 정지해 있다. 카메라가 천천히 push-in한다."

(8) Visual Realism
- 자연스러운 피부결, 미세한 잡티, 옷 주름, 비대칭을 유지한다.
- 물리적 상호작용이 있는 장면에서는 locked-off 또는 느린 push-in을 우선한다.
- Handheld는 피사체와 주요 물체가 거의 정지된 장면에서만 사용한다.

#### 5.  영상 내 상품 텍스트 노출 최소화
- 상품 라벨의 글자와 로고는 식별 가능한 정면 클로즈업으로 보여주지 않는다.
- 상품의 형태, 색상, 용기 구조는 유지하되 라벨은 비가독 상태로 표현한다.
- 상품 라벨은 화면 바깥으로 일부 잘리거나, 손, 소품, 그림자에 의해 부분적으로 가려져야 한다.

#### 6. CTA 구현 규칙
- CTA의 문구 자체는 Visual에 작성하지 않는다.
- CTA에 해당하는 실제 행동이나 화면 연출만 Visual에 작성한다.
- CTA 문구는 voiceover 또는 subtitle 등 별도의 auditory 필드에서 처리한다.
- 마지막 Section의 Visual은 CTA 문구를 직접 표시하지 않고, CTA를 전달할 수 있는 제품 노출 또는 행동을 구성한다.

#### 7. Section 구성 규칙
- 전체 영상은 1~3개 Section으로 구성한다.
- 각 Section에는 하나의 핵심 행동 또는 연속적인 행동 시퀀스만 포함한다.
- 독립적인 행동은 분리하고, 불필요한 물체나 행동을 추가하지 않는다.
- 각 Section은 직전 Section의 마지막 상태에서 자연스럽게 이어진다.
- 짧은 Section을 여러 개 만드는 것보다, 하나의 행동을 충분한 시간 동안 자연스럽게 보여주는 것을 우선한다.
- 물리적으로 복잡한 행동이나 물체 간 상호작용이 필요한 경우, Section 수를 늘리는 대신 해당 행동에 더 긴 Time Range를 할당한다.
- 마지막 Section의 Visual에는 CTA 문구를 작성하지 않는다.
- 마지막 Section의 auditory에는 CTA Action에 부합하는 voiceover 또는 subtitle을 포함한다.

#### 8. Time Range 규칙
- 첫 Section의 start는 반드시 0이다.
- 각 Section의 end는 start보다 커야 한다.
- 이전 Section의 end와 다음 Section의 start는 동일해야 한다.
- Section 사이에 시간 공백이나 중복이 없어야 한다.
- 마지막 Section의 end는 Video duration과 동일해야 한다.

#### 9. Physical Constraint와 상품 사실의 충돌 방지
- 상품의 구조, 기능, 재질, 사용 방법 등 상품 자체에 관한 물리적 특성은 입력된 상품 정보에 없는 내용을 임의로 추론하지 않는다.
- 물리적 연속성을 표현하기 위해 필요한 경우에도 상품 정보에 없는 기능이나 구조를 새롭게 만들어내지 않는다.
- 입력된 상품 정보로 확인할 수 없는 물리적 특성은 단정적으로 표현하지 않는다.

#### 10. 기타
(1) 영상 스크립트 내의 음성 대사는 1초에 3.5음절이 넘지 않도록 한다.

### Methodology

#### 필수 방법론
- 'subtitle'과 'voiceover'의 내용이 동일할 필요는 없습니다.

#### 선택 방법론
- Hook-Body-CTA
- PAS
- AIDA
- BAB(Before-After-Bridge)
- 4Ps(Promise-Picture-Proof-Push)

### 요구사항
- CTA Action: {cta_action}
- Video duration: {request.max_duration_seconds}
- Upload Channel: {request.channel}
= Ads Video Style: 상품의 우수성을 시각적으로 보여주고 싶어서 안달이 난, 인스타그램 인플루언서 내돈내산 릴스 영상 스타일

### 인물 정보
- Character Profile: 차분하지 않음

### 상품 정보
{product_prompt_fields}
{f"\n\n{request.retry_instruction.strip()}" if request.retry_instruction and request.retry_instruction.strip() else ""}
"""


def get_default_script_prompt_preview() -> str:
    """Return the canonical default prompt with runtime values represented as placeholders."""
    return build_script_prompt(
        ScriptGenerationRequest(
            product={
                "selling_point": "{{selling_point}}",
                "usp": "{{usp}}",
                "curator_pitch": "{{curator_pitch}}",
                "hashtags": "{{hashtags}}",
                "description_text": "{{description_text}}",
                "detail_info": "{{detail_info}}",
            },
            reviews=["{{reviews}}"],
            custom_prompt="CTA: {{cta_action}}",
            max_duration_seconds=15,
            channel="{{channel}}",
            target_audience="{{target_audience}}",
        )
    ).strip()

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
    if len(scenes) > 3:
        raise ScriptValidationError("스크립트의 scenes는 최대 3개까지 가능합니다.")

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
        required_fields = (
            "section",
            "visual",
            "auditory",
            "intent",
            "exclusion_list_about_physical_motions",
            "notes",
        )
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
        if len(scene["visual"]) > 300:
            raise ScriptValidationError(f"{index}번째 scene의 visual은 300자 이내여야 합니다.")
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
