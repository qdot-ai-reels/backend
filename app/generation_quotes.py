"""Persisted, transparent pre-generation video cost quotes."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from app.db import GenerationQuoteRow, SessionLocal


DEFAULT_VIDEO_RATE_PER_SECOND_USD = Decimal("0.38")
DEFAULT_MINIMUM_RATE_FACTOR = Decimal("0.95")
DEFAULT_MAXIMUM_RATE_FACTOR = Decimal("1.10")
QUOTE_TTL_MINUTES = 15
QUOTE_COVERAGE = "video_only"
QUOTE_DISCLAIMER = (
    "예상 금액은 영상 provider 비용만 포함합니다. 스크립트 생성, TTS, 렌더링 비용과 "
    "명시적으로 다시 실행한 유료 후보 재시도 비용은 포함하지 않습니다."
)


class GenerationQuoteError(ValueError):
    """Base validation error for a stored generation quote."""


class GenerationQuoteExpiredError(GenerationQuoteError):
    """Raised when a quote is submitted after its fixed TTL."""


class GenerationQuoteMismatchError(GenerationQuoteError):
    """Raised when generation parameters differ from the quoted snapshot."""


@dataclass(frozen=True)
class QuoteSpec:
    template_id: str
    template_version: int
    duration_seconds: int
    candidate_count: int
    visual_mode: str
    resolution: str
    prompt_version_id: str | None = None
    prompt_version: int | None = None
    prompt_version_name: str | None = None
    prompt_content_sha256: str | None = None

    def canonical_payload(self) -> dict[str, Any]:
        payload = {
            "template_id": self.template_id,
            "template_version": self.template_version,
            "duration_seconds": self.duration_seconds,
            "candidate_count": self.candidate_count,
            "visual_mode": self.visual_mode,
            "resolution": self.resolution,
        }
        prompt_fields = (
            self.prompt_version_id,
            self.prompt_version,
            self.prompt_version_name,
            self.prompt_content_sha256,
        )
        if any(value is not None for value in prompt_fields):
            if any(value is None for value in prompt_fields):
                raise GenerationQuoteError(
                    "prompt version 견적 메타데이터가 완전하지 않습니다."
                )
            payload["prompt_version"] = {
                "id": self.prompt_version_id,
                "version": self.prompt_version,
                "name": self.prompt_version_name,
                "content_sha256": self.prompt_content_sha256,
            }
        return payload

    @property
    def request_hash(self) -> str:
        return canonical_request_hash(self.canonical_payload())


def canonical_request_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_decimal_from_env(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as error:
        raise GenerationQuoteError(f"{name}은 숫자여야 합니다.") from error
    if not value.is_finite() or value <= 0:
        raise GenerationQuoteError(f"{name}은 0보다 큰 숫자여야 합니다.")
    return value


def configured_video_rate() -> tuple[Decimal, Decimal, Decimal]:
    expected = _positive_decimal_from_env(
        "VIDEO_RATE_PER_SECOND_USD",
        DEFAULT_VIDEO_RATE_PER_SECOND_USD,
    )
    minimum_factor = _positive_decimal_from_env(
        "VIDEO_QUOTE_MIN_FACTOR",
        DEFAULT_MINIMUM_RATE_FACTOR,
    )
    maximum_factor = _positive_decimal_from_env(
        "VIDEO_QUOTE_MAX_FACTOR",
        DEFAULT_MAXIMUM_RATE_FACTOR,
    )
    if minimum_factor > 1:
        raise GenerationQuoteError("VIDEO_QUOTE_MIN_FACTOR는 1 이하여야 합니다.")
    if maximum_factor < 1:
        raise GenerationQuoteError("VIDEO_QUOTE_MAX_FACTOR는 1 이상이어야 합니다.")
    return (
        expected * minimum_factor,
        expected,
        expected * maximum_factor,
    )


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def create_generation_quote(
    spec: QuoteSpec,
    *,
    model_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    created_at = _as_utc(now or datetime.now(timezone.utc))
    expires_at = created_at + timedelta(minutes=QUOTE_TTL_MINUTES)
    minimum_rate, expected_rate, maximum_rate = configured_video_rate()
    units = Decimal(spec.duration_seconds * spec.candidate_count)
    quote_id = uuid.uuid4().hex
    row = GenerationQuoteRow(
        quote_id=quote_id,
        request_hash=spec.request_hash,
        template_id=spec.template_id,
        template_version=spec.template_version,
        duration_seconds=spec.duration_seconds,
        candidate_count=spec.candidate_count,
        visual_mode=spec.visual_mode,
        resolution=spec.resolution,
        model_id=model_id,
        prompt_version_id=spec.prompt_version_id,
        prompt_version=spec.prompt_version,
        prompt_version_name=spec.prompt_version_name,
        prompt_content_sha256=spec.prompt_content_sha256,
        currency="USD",
        rate_per_second_usd=_money(expected_rate),
        minimum_cost_usd=_money(minimum_rate * units),
        expected_cost_usd=_money(expected_rate * units),
        maximum_cost_usd=_money(maximum_rate * units),
        rate_source="configured_rate_card",
        coverage=QUOTE_COVERAGE,
        disclaimer=QUOTE_DISCLAIMER,
        created_at=created_at,
        expires_at=expires_at,
    )
    with SessionLocal() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return quote_to_public(row)


def get_generation_quote(quote_id: str) -> GenerationQuoteRow | None:
    with SessionLocal() as session:
        row = session.get(GenerationQuoteRow, quote_id)
        if row is None:
            return None
        # Detach the immutable snapshot before the session closes.
        session.expunge(row)
        return row


def validate_generation_quote(
    quote_id: str,
    spec: QuoteSpec,
    *,
    model_id: str | None = None,
    prompt_version_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    quote = get_generation_quote(quote_id)
    if quote is None:
        raise GenerationQuoteError("비용 견적을 찾을 수 없습니다.")
    current_time = _as_utc(now or datetime.now(timezone.utc))
    if _as_utc(quote.expires_at) <= current_time:
        raise GenerationQuoteExpiredError("비용 견적이 만료되었습니다. 다시 계산해 주세요.")
    if (
        not quote.prompt_version_id
        or quote.prompt_version is None
        or not quote.prompt_version_name
        or not quote.prompt_content_sha256
    ):
        raise GenerationQuoteMismatchError(
            "이 견적에는 production prompt version이 고정되어 있지 않습니다. 다시 계산해 주세요."
        )
    if prompt_version_id is not None and quote.prompt_version_id != prompt_version_id:
        raise GenerationQuoteMismatchError(
            "견적의 prompt version과 생성 요청의 prompt version이 다릅니다. 다시 계산해 주세요."
        )
    quoted_spec = replace(
        spec,
        prompt_version_id=quote.prompt_version_id,
        prompt_version=quote.prompt_version,
        prompt_version_name=quote.prompt_version_name,
        prompt_content_sha256=quote.prompt_content_sha256,
    )
    if quote.request_hash != quoted_spec.request_hash or (
        model_id is not None and quote.model_id != model_id
    ):
        raise GenerationQuoteMismatchError(
            "견적을 계산한 조건과 영상 생성 조건이 다릅니다. 다시 계산해 주세요."
        )
    return quote_to_public(quote)


def quote_to_public(row: GenerationQuoteRow) -> dict[str, Any]:
    quantity_seconds = row.duration_seconds * row.candidate_count
    expected_rate = Decimal(str(row.rate_per_second_usd))
    minimum_total = Decimal(str(row.minimum_cost_usd))
    expected_total = Decimal(str(row.expected_cost_usd))
    maximum_total = Decimal(str(row.maximum_cost_usd))
    minimum_rate = minimum_total / Decimal(quantity_seconds)
    maximum_rate = maximum_total / Decimal(quantity_seconds)
    prompt_version = None
    if (
        row.prompt_version_id
        and row.prompt_version is not None
        and row.prompt_version_name
        and row.prompt_content_sha256
    ):
        prompt_version = {
            "id": row.prompt_version_id,
            "version": row.prompt_version,
            "name": row.prompt_version_name,
            "content_sha256": row.prompt_content_sha256,
        }
    return {
        "quote_id": row.quote_id,
        "template": {
            "id": row.template_id,
            "version": row.template_version,
            "duration_seconds": row.duration_seconds,
        },
        "candidate_count": row.candidate_count,
        "visual_mode": row.visual_mode,
        "model": {"id": row.model_id, "resolution": row.resolution},
        "prompt_version": prompt_version,
        "currency": row.currency,
        "line_items": [
            {
                "kind": "video_generation",
                "quantity": quantity_seconds,
                "unit": "second_candidate",
                "unit_price_min": float(_money(minimum_rate)),
                "unit_price_expected": float(_money(expected_rate)),
                "unit_price_max": float(_money(maximum_rate)),
                "subtotal_min": float(minimum_total),
                "subtotal_expected": float(expected_total),
                "subtotal_max": float(maximum_total),
                "source": row.rate_source,
            }
        ],
        "total": {
            "min": float(minimum_total),
            "expected": float(expected_total),
            "max": float(maximum_total),
        },
        "coverage": row.coverage,
        "automatic_paid_retries": 0,
        "candidate_retry_policy": {
            "authorized_paid_retries": 0,
            "cost_included_in_total": False,
        },
        "disclaimer": row.disclaimer,
        "created_at": _as_utc(row.created_at).isoformat(),
        "expires_at": _as_utc(row.expires_at).isoformat(),
    }
