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
                input_type=input_type,
                product_json=json.dumps(product, ensure_ascii=False) if product else None,
                script_json=json.dumps(script, ensure_ascii=False) if script else None,
                image_url=image_url,
            )
        )
        session.commit()


def update_job(job_id: str, **values: Any) -> None:
    allowed = {
        "status", "script_json", "video_job_id", "caption_job_id",
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
        return {
            "job_id": row.job_id,
            "status": row.status,
            "input_type": row.input_type,
            "script": json.loads(row.script_json) if row.script_json else None,
            "video_job_id": row.video_job_id,
            "caption_job_id": row.caption_job_id,
            "output_path": row.output_path,
            "error": row.error_message,
            "cost": row.cost,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
