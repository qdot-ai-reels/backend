"""Generate structured short-form scripts through the OpenRouter chat API."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b:free"
DEFAULT_FALLBACK_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
DEFAULT_SYLLABLES_PER_SECOND = 4.5


class OpenRouterError(RuntimeError):
    """Base error for OpenRouter request and response failures."""


class OpenRouterConfigurationError(OpenRouterError):
    """Raised when required OpenRouter configuration is missing."""


class OpenRouterRequestError(OpenRouterError):
    """Raised when OpenRouter rejects or cannot receive a request."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
    max_duration_seconds: int = 30
    channel: str = "Instagram Reels"
    target_audience: str = "육아에 관심 있는 보호자"


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
    return f"""당신은 공동구매 광고 숏폼 스크립트 작성자입니다.

아래 상품 데이터에 실제로 포함된 정보만 사용해 {request.channel}용 스크립트를 작성하세요.
확인되지 않은 효능, 인증, 소재, 가격, 할인, 사용 후기 또는 제품 특징을 추측해서 추가하지 마세요.
상품 데이터에 없는 정보는 빈 문자열 또는 빈 배열로 두세요.

필수 조건:
- 세로형 9:16 영상
- 최대 {request.max_duration_seconds}초
- Hook, Body, CTA 흐름
- 장면마다 하나의 핵심 행동
- 영상 없이 자막만 읽어도 이해 가능
- 첫 장면은 시선을 끌고, 마지막 장면은 구체적인 CTA를 포함
- 자막은 짧게 작성하고 화면에 넣을 문구와 내레이션을 구분
- 대사는 장면 시간 안에 읽을 수 있도록 작성하고, 평균 1초당 4.5음절을 기준으로 계산
- 각 장면의 대사 음절 수가 해당 장면 시간 x 4.5를 넘지 않도록 작성
{"- USP가 비어있거나 null인 경우, 상품 데이터의 다른 정보만을 바탕으로 소비자의 구매를 유도할 수 있는 핵심 소구점(USP)을 도출해 최종 스크립트에 반영하세요." if not has_usp else ""}

다음 JSON 객체만 반환하세요. Markdown 코드블록이나 설명은 붙이지 마세요.
{{
  "meta": {{
    "aspect_ratio": "9:16",
    "max_duration_sec": {request.max_duration_seconds},
    "channel": "{request.channel}"
  }},
  "summary": "스크립트 전체 방향",
  "scenes": [
    {{
      "scene_number": 1,
      "time_range_sec": [0, 3],
      "visual": "화면에 보일 장면과 행동",
      "subtitle": "화면 자막",
      "voiceover": "내레이션",
      "intent": "hook|body|cta"
    }}
  ],
  "compliance_notes": ["사용한 근거와 주의사항"]
}}

타깃: {request.target_audience}
상품 데이터:
{product_json}
"""


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

    scenes = document.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ScriptValidationError("스크립트에는 하나 이상의 scenes가 필요합니다.")

    meta = document.get("meta")
    if not isinstance(meta, Mapping) or meta.get("aspect_ratio") != "9:16":
        raise ScriptValidationError("스크립트의 aspect_ratio는 9:16이어야 합니다.")
    if not isinstance(meta.get("max_duration_sec"), (int, float)):
        raise ScriptValidationError("스크립트의 max_duration_sec가 필요합니다.")
    if (
        max_duration_seconds is not None
        and meta["max_duration_sec"] != max_duration_seconds
    ):
        raise ScriptValidationError(
            "스크립트의 max_duration_sec가 요청한 최대 시간과 다릅니다."
        )
    if not isinstance(meta.get("channel"), str) or not meta["channel"].strip():
        raise ScriptValidationError("스크립트의 channel이 필요합니다.")
    if not isinstance(document.get("summary"), str):
        raise ScriptValidationError("스크립트의 summary가 필요합니다.")
    if not isinstance(document.get("compliance_notes"), list):
        raise ScriptValidationError("스크립트의 compliance_notes가 필요합니다.")

    previous_end = 0.0
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, Mapping):
            raise ScriptValidationError(f"{index}번째 scene이 JSON 객체가 아닙니다.")
        time_range = scene.get("time_range_sec")
        if (
            not isinstance(time_range, list)
            or len(time_range) != 2
            or not all(isinstance(value, (int, float)) for value in time_range)
            or time_range[0] < 0
            or time_range[1] <= time_range[0]
            or time_range[0] < previous_end
            or (
                max_duration_seconds is not None
                and time_range[1] > max_duration_seconds
            )
        ):
            raise ScriptValidationError(
                f"{index}번째 scene의 time_range_sec가 올바르지 않습니다."
            )
        required_fields = ("scene_number", "visual", "subtitle", "voiceover", "intent")
        if any(field not in scene for field in required_fields):
            raise ScriptValidationError(
                f"{index}번째 scene에 필수 출력 필드가 누락되었습니다."
            )
        if not isinstance(scene.get("scene_number"), int):
            raise ScriptValidationError(f"{index}번째 scene의 scene_number가 올바르지 않습니다.")
        if not isinstance(scene.get("visual"), str) or not scene["visual"].strip():
            raise ScriptValidationError(
                f"{index}번째 scene의 visual이 필요합니다."
            )
        if not isinstance(scene.get("subtitle"), str) or not scene["subtitle"].strip():
            raise ScriptValidationError(f"{index}번째 scene의 subtitle이 필요합니다.")
        if scene.get("voiceover") is not None and not isinstance(scene["voiceover"], str):
            raise ScriptValidationError(
                f"{index}번째 scene의 voiceover는 문자열 또는 null이어야 합니다."
            )
        if scene.get("intent") not in {"hook", "body", "cta"}:
            raise ScriptValidationError(
                f"{index}번째 scene의 intent는 hook, body, cta 중 하나여야 합니다."
            )
        previous_end = float(time_range[1])

    validate_dialogue_lengths(document)

    return dict(document)


def count_speech_syllables(text: str) -> int:
    """Count spoken characters while ignoring whitespace and punctuation."""
    return sum(1 for character in text if character.isalnum())


def validate_dialogue_lengths(
    document: Mapping[str, Any],
    syllables_per_second: float = DEFAULT_SYLLABLES_PER_SECOND,
) -> None:
    """Validate dialogue length before the script is passed to later tasks."""
    if syllables_per_second <= 0:
        raise ValueError("syllables_per_second는 0보다 커야 합니다.")

    scenes = document.get("scenes") or []
    for index, scene in enumerate(scenes, start=1):
        voiceover = scene.get("voiceover")
        if not isinstance(voiceover, str) or not voiceover.strip():
            continue
        start, end = scene["time_range_sec"]
        max_syllables = max(1, int((end - start) * syllables_per_second))
        actual_syllables = count_speech_syllables(voiceover)
        if actual_syllables > max_syllables:
            raise ScriptDialogueLengthError(
                scene_number=int(scene.get("scene_number", index)),
                max_syllables=max_syllables,
                actual_syllables=actual_syllables,
            )


class OpenRouterClient:
    """Small dependency-injectable client for OpenRouter script generation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_model: str | None = DEFAULT_FALLBACK_MODEL,
        api_url: str = DEFAULT_API_URL,
        timeout_seconds: int = 60,
        max_attempts: int = 2,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts는 1 이상이어야 합니다.")
        self.api_key = api_key
        self.model = model
        self.fallback_model = fallback_model
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.opener = opener

    @classmethod
    def from_env(cls) -> "OpenRouterClient":
        return cls(
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            model=os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL,
            fallback_model=os.getenv("OPENROUTER_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL,
            api_url=os.getenv("OPENROUTER_API_URL") or DEFAULT_API_URL,
        )

    def generate_script(self, request: ScriptGenerationRequest) -> dict[str, Any]:
        if not self.api_key:
            raise OpenRouterConfigurationError("OPENROUTER_API_KEY가 설정되지 않았습니다.")
        if not self.model:
            raise OpenRouterConfigurationError("OPENROUTER_MODEL이 설정되지 않았습니다.")

        last_error: OpenRouterError | None = None
        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)

        for attempt in range(min(self.max_attempts, len(models))):
            try:
                return self._generate_once(request, models[attempt], attempt)
            except ScriptValidationError as error:
                last_error = error
            except OpenRouterRequestError as error:
                if error.status_code not in (429, 503):
                    raise
                last_error = error

        assert last_error is not None
        raise last_error

    def _generate_once(
        self, request: ScriptGenerationRequest, model: str, attempt: int
    ) -> dict[str, Any]:
        retry_instruction = ""
        if attempt > 0:
            retry_instruction = (
                "\n이전 모델 응답이 형식 검증에 실패했습니다. "
                "aspect_ratio는 반드시 정확히 9:16으로 작성하고, 상품 데이터에 없는 장면이나 "
                "효능을 만들지 마세요. JSON만 반환하세요.\n"
            )

        payload = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 2000,
            "reasoning": {"exclude": True},
            "messages": [{
                "role": "user",
                "content": build_script_prompt(request) + retry_instruction,
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

        try:
            with self.opener(http_request, timeout=self.timeout_seconds) as response:
                response_body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise OpenRouterRequestError(
                f"OpenRouter 요청이 거부되었습니다. HTTP {error.code}",
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
            extract_script_json(content),
            max_duration_seconds=request.max_duration_seconds,
        )
