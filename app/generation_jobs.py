"""Persistence helpers for the end-to-end reel generation jobs."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select

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
    client_request_id: str | None = None,
    request_hash: str | None = None,
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
                client_request_id=client_request_id,
                request_hash=request_hash,
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
        return _job_response(row) if row is not None else None


def get_job_idempotency(client_request_id: str) -> tuple[str, str | None] | None:
    """Resolve one public idempotency key without exposing the persisted payload."""
    with SessionLocal() as session:
        row = session.scalar(
            select(GenerationJobRow).where(
                GenerationJobRow.client_request_id == client_request_id
            )
        )
        if row is None:
            return None
        return row.job_id, row.request_hash


def list_generation_jobs(
    *,
    limit: int = 24,
    cursor: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Return an opaque-cursor page containing public, management-safe summaries."""
    statement = select(GenerationJobRow)
    if status:
        statement = statement.where(GenerationJobRow.status == status)
    if cursor:
        cursor_created_at, cursor_job_id = _decode_cursor(cursor)
        statement = statement.where(
            or_(
                GenerationJobRow.created_at < cursor_created_at,
                and_(
                    GenerationJobRow.created_at == cursor_created_at,
                    GenerationJobRow.job_id < cursor_job_id,
                ),
            )
        )
    statement = statement.order_by(
        GenerationJobRow.created_at.desc(), GenerationJobRow.job_id.desc()
    ).limit(limit + 1)
    with SessionLocal() as session:
        rows = list(session.scalars(statement))
        has_more = len(rows) > limit
        page = rows[:limit]
        items = [_job_summary(row) for row in page]
    next_cursor = None
    if has_more and page:
        next_cursor = _encode_cursor(page[-1].created_at, page[-1].job_id)
    return {"items": items, "next_cursor": next_cursor}


def _job_response(row: GenerationJobRow) -> dict[str, Any]:
    error_code, retryable = _error_metadata(row.status, row.stage, row.error_message)
    candidates = _json_list(row.candidates_json)
    completed_candidates = sum(
        item.get("status") == "COMPLETED" for item in candidates
    )
    failed_candidates = sum(item.get("status") == "FAILED" for item in candidates)
    response = {
        "job_id": row.job_id,
        "status": row.status,
        "stage": row.stage,
        "input_type": row.input_type,
        "script": _json_dict(row.script_json),
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
    response.update(_workflow_provenance(row.payload_json))
    summary = _job_summary(row)
    response["product"] = summary["product"]
    response["duration_seconds"] = summary["duration_seconds"]
    response["asset_fidelity"] = summary["asset_fidelity"]
    response["cost_summary"] = summary["cost"]
    return response


def _job_summary(row: GenerationJobRow) -> dict[str, Any]:
    payload = _json_dict(row.payload_json) or {}
    product_document = _json_dict(getattr(row, "product_json", None)) or {}
    product = (
        product_document.get("product")
        if isinstance(product_document.get("product"), dict)
        else product_document
    )
    candidates = _json_list(row.candidates_json)
    completed = [item for item in candidates if item.get("status") == "COMPLETED"]
    failed = [item for item in candidates if item.get("status") == "FAILED"]
    primary = completed[0] if completed else None
    script = _json_dict(row.script_json)
    template = _template_summary(payload, script)
    error_code, retryable = _error_metadata(row.status, row.stage, row.error_message)
    quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else {}
    quote_total = quote.get("total") if isinstance(quote.get("total"), dict) else {}
    product_id = _first_string(product, "product_id", "id", "uuid")
    product_name = _first_string(product, "product_name", "name", "title") or "이름 없는 상품"
    thumbnail_url = getattr(row, "image_url", None) or _first_string(
        product, "image_url", "thumbnail_url"
    )
    package_text_verified = bool(
        product.get("package_text_verified")
        or product.get("verified_package_claims")
        or (
            isinstance(product.get("asset_fidelity"), dict)
            and product["asset_fidelity"].get("package_text_verified")
        )
    )
    primary_candidate = None
    if primary is not None:
        validation = primary.get("validation") if isinstance(primary.get("validation"), dict) else {}
        candidate_id = str(primary.get("candidate_id"))
        primary_candidate = {
            "candidate_id": candidate_id,
            "video_url": f"/api/v1/reels/generate/{row.job_id}/candidates/{candidate_id}/file",
            "download_url": (
                f"/api/v1/reels/generate/{row.job_id}/candidates/"
                f"{candidate_id}/file?download=true"
            ),
            "duration_seconds": validation.get("duration_seconds"),
            "technical_score": validation.get("technical_score"),
        }
    return {
        "job_id": row.job_id,
        "product": {
            "id": product_id,
            "name": product_name,
            "thumbnail_url": thumbnail_url,
        },
        "status": row.status,
        "stage": row.stage,
        "template": template,
        "duration_seconds": template.get("duration_seconds") if template else _script_duration(script),
        "visual_mode": _visual_provenance(row.payload_json).get("visual_mode", "product_only"),
        "candidates": {
            "total": row.candidate_count if row.candidate_count is not None else len(candidates),
            "completed": len(completed),
            "failed": len(failed),
        },
        "cost": {
            "currency": quote.get("currency", "USD"),
            "estimated_min": quote_total.get("min"),
            "estimated_expected": quote_total.get("expected"),
            "estimated_max": quote_total.get("max"),
            "actual": row.cost,
            "coverage": quote.get("coverage", "video_only"),
        },
        "primary_candidate": primary_candidate,
        "error": (
            {
                "code": error_code,
                "message": row.error_message,
                "retryable": retryable,
            }
            if row.error_message
            else None
        ),
        "asset_fidelity": {
            "package_text_verified": package_text_verified,
            "warning": (
                None
                if package_text_verified
                else "패키지 수량과 작은 글자는 원본 packshot으로 최종 확인해야 합니다."
            ),
        },
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _json_dict(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _json_list(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _first_string(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _script_duration(script: dict[str, Any] | None) -> float | None:
    try:
        value = script["scenes"][-1]["time_range_sec"]["end"] if script else None
        return float(value) if isinstance(value, (int, float)) else None
    except (KeyError, IndexError, TypeError):
        return None


def _template_summary(
    payload: dict[str, Any], script: dict[str, Any] | None
) -> dict[str, Any] | None:
    template = payload.get("template")
    if isinstance(template, dict) and isinstance(template.get("id"), str):
        return {
            "id": template["id"],
            "version": template.get("version"),
            "duration_seconds": template.get("duration_seconds") or _script_duration(script),
        }
    template_id = payload.get("template_id")
    if isinstance(template_id, str) and template_id:
        return {
            "id": template_id,
            "version": payload.get("template_version"),
            "duration_seconds": payload.get("max_duration_seconds") or _script_duration(script),
        }
    return None


def _workflow_provenance(payload_json: str | None) -> dict[str, Any]:
    payload = _json_dict(payload_json) or {}
    template = _template_summary(payload, None)
    quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else None
    result: dict[str, Any] = {}
    if template is not None:
        result["template"] = template
    if quote is not None:
        result["quote"] = {
            "quote_id": quote.get("quote_id"),
            "currency": quote.get("currency"),
            "total": quote.get("total"),
            "coverage": quote.get("coverage"),
            "expires_at": quote.get("expires_at"),
        }
    return result


def _encode_cursor(created_at: datetime, job_id: str) -> str:
    raw = json.dumps(
        {"created_at": created_at.isoformat(), "job_id": job_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        created_at = datetime.fromisoformat(payload["created_at"])
        job_id = payload["job_id"]
        if not isinstance(job_id, str) or not job_id:
            raise ValueError
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        binascii.Error,
    ) as error:
        raise ValueError("목록 cursor가 올바르지 않습니다.") from error
    return created_at, job_id


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
