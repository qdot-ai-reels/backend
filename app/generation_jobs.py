"""Persistence helpers for the end-to-end reel generation jobs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.db import GenerationJobRow, SessionLocal


def create_job(
    job_id: str,
    *,
    input_type: str,
    product: dict[str, Any] | None,
    script: dict[str, Any] | None,
    image_url: str | None,
    payload: dict[str, Any] | None = None,
    candidate_count: int = 0,
) -> None:
    candidates = [
        {
            "candidate_id": f"candidate-{index:02d}",
            "index": index,
            "status": "PENDING",
            "stage": "QUEUED",
            "provider_job_id": None,
            "caption_job_id": None,
            "output_path": None,
            "attempts": 0,
            "cost": 0.0,
            "validation": None,
            "error": None,
            "error_code": None,
            "retryable": None,
        }
        for index in range(1, candidate_count + 1)
    ]
    with SessionLocal() as session:
        session.add(
            GenerationJobRow(
                job_id=job_id,
                status="PENDING",
                stage="QUEUED",
                input_type=input_type,
                product_json=json.dumps(product, ensure_ascii=False) if product else None,
                script_json=json.dumps(script, ensure_ascii=False) if script else None,
                payload_json=(
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None
                ),
                image_url=image_url,
                candidate_count=candidate_count,
                candidates_json=json.dumps(candidates, ensure_ascii=False),
            )
        )
        session.commit()


def update_job(job_id: str, **values: Any) -> None:
    allowed = {
        "status", "stage", "script_json", "video_job_id", "caption_job_id",
        "output_path", "error_message", "cost", "payload_json", "candidate_count",
        "candidates_json",
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


def update_candidate(
    job_id: str,
    candidate_id: str,
    *,
    expected_status: str | None = None,
    **values: Any,
) -> dict[str, Any]:
    """Atomically update one persisted candidate record."""
    allowed = {
        "status",
        "stage",
        "provider_job_id",
        "caption_job_id",
        "output_path",
        "attempts",
        "cost",
        "validation",
        "error",
        "error_code",
        "retryable",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"지원하지 않는 후보 상태 필드입니다: {sorted(unknown)}")

    with SessionLocal() as session:
        row = session.scalar(
            select(GenerationJobRow)
            .where(GenerationJobRow.job_id == job_id)
            .with_for_update()
        )
        if row is None:
            raise ValueError(f"작업을 찾을 수 없습니다: {job_id}")
        candidates = json.loads(row.candidates_json or "[]")
        candidate = next(
            (item for item in candidates if item.get("candidate_id") == candidate_id),
            None,
        )
        if candidate is None:
            raise ValueError(f"영상 후보를 찾을 수 없습니다: {candidate_id}")
        if expected_status is not None and candidate.get("status") != expected_status:
            raise ValueError(
                f"영상 후보 상태가 {expected_status}이(가) 아닙니다: "
                f"{candidate.get('status')}"
            )
        candidate.update(values)
        row.candidates_json = json.dumps(candidates, ensure_ascii=False)
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
        return candidate


def get_job(job_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        row = session.get(GenerationJobRow, job_id)
        if row is None:
            return None
        error_code, retryable = _error_metadata(row.status, row.stage, row.error_message)
        candidates = json.loads(row.candidates_json or "[]")
        completed_candidates = sum(
            item.get("status") == "COMPLETED" for item in candidates
        )
        failed_candidates = sum(item.get("status") == "FAILED" for item in candidates)
        response = {
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
            "candidate_count": (
                row.candidate_count
                if row.candidate_count is not None
                else len(candidates)
            ),
            "completed_candidates": completed_candidates,
            "failed_candidates": failed_candidates,
            "candidates": candidates,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "elapsed_seconds": _elapsed_seconds(row.created_at, row.updated_at, row.status),
            "message": _status_message(row.status, row.stage, row.input_type),
        }
        response.update(_visual_provenance(row.payload_json))
        return response


def get_job_payload(job_id: str) -> dict[str, Any] | None:
    """Load the private persisted payload used by worker/retry code only."""
    with SessionLocal() as session:
        row = session.get(GenerationJobRow, job_id)
        if row is None or not row.payload_json:
            return None
        payload = json.loads(row.payload_json)
        return payload if isinstance(payload, dict) else None


def _visual_provenance(payload_json: str | None) -> dict[str, Any]:
    """Expose only a safe visual-mode summary from the private job payload."""
    if not payload_json:
        return {}
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    references: list[str] = []
    multiple = payload.get("influencer_image_urls")
    if isinstance(multiple, list):
        references = [
            value.strip()
            for value in multiple
            if isinstance(value, str) and value.strip()
        ]
    if not references:
        legacy = payload.get("influencer_image_url")
        if isinstance(legacy, str) and legacy.strip():
            references = [legacy.strip()]

    # Preserve reference order while preventing duplicate URLs from inflating
    # the count. The URL values never leave this helper.
    reference_count = min(2, len(dict.fromkeys(references)))
    explicit_mode = payload.get("visual_mode")
    visual_mode = (
        explicit_mode
        if explicit_mode in {"product_only", "model_included", "generated_model"}
        else ("model_included" if reference_count else "product_only")
    )
    return {
        "visual_mode": visual_mode,
        "influencer_reference_count": reference_count,
    }


def _elapsed_seconds(created_at, updated_at, status: str) -> float | None:
    if created_at is None:
        return None
    end = (
        updated_at
        if status in {"COMPLETED", "PARTIAL_COMPLETED", "FAILED"}
        else datetime.now(timezone.utc)
    )
    start = created_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return round(max(0.0, (end - start).total_seconds()), 2)


def _status_message(status: str, stage: str | None, input_type: str = "video") -> str:
    if status == "COMPLETED":
        if input_type == "script":
            return "스크립트 생성이 완료되었습니다."
        return "최종 영상 생성이 완료되었습니다."
    if status == "FAILED":
        return "최종 영상 생성을 완료하지 못했습니다."
    if status == "PARTIAL_COMPLETED":
        return "일부 영상 후보 생성이 완료되었습니다."
    return {
        "QUEUED": "생성 작업을 준비하고 있습니다.",
        "SCRIPT_GENERATION": "스크립트를 생성하고 있습니다.",
        "SCRIPT_REGENERATION": "음성 길이에 맞게 스크립트를 다시 생성하고 있습니다.",
        "TTS_GENERATION": "음성을 생성하고 있습니다.",
        "TTS_VALIDATION": "장면별 음성 길이를 확인하고 있습니다.",
        "TTS_FALLBACK": "길이가 초과된 장면의 음성만 안전하게 조정하고 있습니다.",
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
    if (
        "inputimagesensitivecontentdetected" in message
        or "privacyinformation" in message
    ):
        # Candidate finalization may collapse the top-level stage to FAILED;
        # keep this provider input rejection stable at both response levels.
        return "VIDEO_INPUT_INVALID", False
    if stage in {"SCRIPT_GENERATION", "SCRIPT_REGENERATION"}:
        if "no endpoints available" in message:
            return "SCRIPT_PROVIDER_UNAVAILABLE", True
        if "openrouter" in message:
            return "SCRIPT_PROVIDER_ERROR", True
        return "SCRIPT_GENERATION_FAILED", False
    if stage in {"TTS_GENERATION", "TTS_VALIDATION", "TTS_FALLBACK"}:
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
