"""Persistence helpers for the end-to-end reel generation jobs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.db import GenerationJobRow, SessionLocal


def create_job(
    job_id: str,
    *,
    input_type: str,
    product: dict[str, Any] | None,
    script: dict[str, Any] | None,
    image_url: str | None,
) -> None:
    with SessionLocal() as session:
        session.add(
            GenerationJobRow(
                job_id=job_id,
                status="PENDING",
                stage="QUEUED",
                input_type=input_type,
                product_json=json.dumps(product, ensure_ascii=False) if product else None,
                script_json=json.dumps(script, ensure_ascii=False) if script else None,
                image_url=image_url,
            )
        )
        session.commit()


def update_job(job_id: str, **values: Any) -> None:
    allowed = {
        "status", "stage", "script_json", "video_job_id", "caption_job_id",
        "output_path", "error_message", "cost",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"지원하지 않는 작업 상태 필드입니다: {sorted(unknown)}")

    with SessionLocal() as session:
        row = session.get(GenerationJobRow, job_id)
        if row is None:
            raise ValueError(f"작업을 찾을 수 없습니다: {job_id}")
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = datetime.now(timezone.utc)
        session.commit()


def get_job(job_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        row = session.get(GenerationJobRow, job_id)
        if row is None:
            return None
        error_code, retryable = _error_metadata(row.status, row.stage, row.error_message)
        return {
            "job_id": row.job_id,
            "status": row.status,
            "stage": row.stage,
            "input_type": row.input_type,
            "script": json.loads(row.script_json) if row.script_json else None,
            "video_job_id": row.video_job_id,
            "caption_job_id": row.caption_job_id,
            "output_path": row.output_path,
            "error": row.error_message,
            "error_code": error_code,
            "retryable": retryable,
            "cost": row.cost,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "elapsed_seconds": _elapsed_seconds(row.created_at, row.updated_at, row.status),
            "message": _status_message(row.status, row.stage),
        }


def _elapsed_seconds(created_at, updated_at, status: str) -> float | None:
    if created_at is None:
        return None
    end = updated_at if status in {"COMPLETED", "FAILED"} else datetime.now(timezone.utc)
    start = created_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return round(max(0.0, (end - start).total_seconds()), 2)


def _status_message(status: str, stage: str | None) -> str:
    if status == "COMPLETED":
        return "최종 영상 생성이 완료되었습니다."
    if status == "FAILED":
        return "최종 영상 생성을 완료하지 못했습니다."
    return {
        "QUEUED": "생성 작업을 준비하고 있습니다.",
        "SCRIPT_GENERATION": "스크립트를 생성하고 있습니다.",
        "SCRIPT_REGENERATION": "음성 길이에 맞게 스크립트를 다시 생성하고 있습니다.",
        "TTS_GENERATION": "음성을 생성하고 있습니다.",
        "TTS_VALIDATION": "장면별 음성 길이를 확인하고 있습니다.",
        "VIDEO_GENERATION": "영상 생성 서버에서 영상을 만들고 있습니다.",
        "AUDIO_MERGE": "영상과 음성을 결합하고 있습니다.",
        "CAPTION_RENDER": "Caption을 적용하고 있습니다.",
    }.get(stage or "", "최종 영상을 생성하고 있습니다.")


def _error_metadata(
    status: str, stage: str | None, error_message: str | None
) -> tuple[str | None, bool | None]:
    """Return stable UI metadata without changing the existing DB schema."""
    if status != "FAILED":
        return None, None

    message = (error_message or "").lower()
    if stage in {"SCRIPT_GENERATION", "SCRIPT_REGENERATION"}:
        if "no endpoints available" in message:
            return "SCRIPT_PROVIDER_UNAVAILABLE", True
        if "openrouter" in message:
            return "SCRIPT_PROVIDER_ERROR", True
        return "SCRIPT_GENERATION_FAILED", False
    if stage in {"TTS_GENERATION", "TTS_VALIDATION"}:
        if "음성이 너무 깁니다" in (error_message or ""):
            return "TTS_SCENE_TOO_LONG", True
        return "TTS_GENERATION_FAILED", False
    if stage == "VIDEO_GENERATION":
        if "no endpoints available" in message:
            return "VIDEO_PROVIDER_UNAVAILABLE", True
        if "시간이 초과" in (error_message or "") or "timeout" in message:
            return "VIDEO_PROVIDER_TIMEOUT", True
        if "이미지" in (error_message or "") or "format" in message:
            return "VIDEO_INPUT_INVALID", False
        return "VIDEO_GENERATION_FAILED", False
    if stage == "AUDIO_MERGE":
        return "AUDIO_MERGE_FAILED", False
    if stage == "CAPTION_RENDER":
        return "CAPTION_RENDER_FAILED", False
    return "GENERATION_FAILED", False
