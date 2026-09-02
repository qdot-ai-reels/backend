from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.image_metadata import validate_image_inputs
from app.script_generator import (
    OpenRouterConfigurationError,
    OpenRouterRequestError,
    ScriptGenerationRequest,
    ScriptValidationError,
)
from app.api.v1.settings import get_optional_settings_repository
from app.runtime_config import build_script_client, resolve_script_generation_duration
from app.generation_jobs import create_job, get_job, update_job
from app.settings_service import ProviderCatalogError
from app.settings_service import SettingsService


router = APIRouter()
logger = logging.getLogger(__name__)


class ScriptGenerationBody(BaseModel):
    product: dict[str, Any] = Field(min_length=1)
    image_url: str | None = None
    reviews: list[Any] = Field(default_factory=list)
    prompt: str | None = None
    max_duration_seconds: int | None = Field(default=None, ge=1, le=30)
    channel: str = "Instagram Reels"
    target_audience: str = "육아에 관심 있는 보호자"


def validate_product_image_inputs(product: dict[str, Any], image_url: str | None) -> None:
    raw_product = product.get("product") if isinstance(product.get("product"), dict) else product
    primary_image_url = image_url or raw_product.get("image_url") or product.get("image_url")
    validate_image_inputs(image_url=primary_image_url)


def _script_error_detail(error: Exception) -> dict[str, Any]:
    message = str(error)
    if isinstance(error, OpenRouterRequestError) and error.status_code == 404 and "No endpoints available" in message:
        code = "SCRIPT_PROVIDER_UNAVAILABLE"
        retryable = True
    elif isinstance(error, OpenRouterRequestError):
        code = "SCRIPT_PROVIDER_ERROR"
        retryable = True
    elif isinstance(error, ScriptValidationError):
        code = "SCRIPT_RESPONSE_INVALID"
        retryable = False
    else:
        code = "SCRIPT_GENERATION_FAILED"
        retryable = False
    return {
        "stage": "SCRIPT_GENERATION",
        "error_code": code,
        "retryable": retryable,
        "message": message,
    }


@router.post(
    "/script",
    status_code=status.HTTP_202_ACCEPTED,
    summary="공구 상품 정보를 바탕으로 광고 스크립트 생성",
)
def generate_script(
    body: ScriptGenerationBody,
    background_tasks: BackgroundTasks,
    service: SettingsService | None = Depends(get_optional_settings_repository),
) -> dict[str, Any]:
    if not isinstance(service, SettingsService):
        service = None
    try:
        validate_product_image_inputs(body.product, body.image_url)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        # Resolve settings here so invalid configuration is reported before a job
        # is created, while the provider request itself runs after the response.
        if service is not None:
            service.get_runtime_settings()
    except (OpenRouterConfigurationError, ProviderCatalogError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_script_error_detail(error)) from error

    job_id = uuid.uuid4().hex
    create_job(
        job_id,
        input_type="script",
        product=body.product,
        script=None,
        image_url=body.image_url,
    )
    background_tasks.add_task(run_script_job, job_id, body.model_dump())
    return {
        "job_id": job_id,
        "status": "PENDING",
        "status_url": f"/api/v1/reels/script/{job_id}",
    }


@router.get(
    "/script/{job_id}",
    status_code=status.HTTP_200_OK,
    summary="스크립트 생성 작업 상태 조회",
)
def get_script_status(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None or job.get("input_type") != "script":
        raise HTTPException(status_code=404, detail="스크립트 생성 작업을 찾을 수 없습니다.")
    return job


def run_script_job(job_id: str, payload: dict[str, Any]) -> None:
    """Generate a script without holding the initial HTTP request open."""
    update_job(job_id, status="PROCESSING", stage="SCRIPT_GENERATION")
    session = None
    try:
        service, session = _build_settings_service()
        max_duration_seconds = payload.get("max_duration_seconds") or (
            service.get_runtime_settings().video_max_duration_seconds if service else 15
        )
        max_duration_seconds, supported_durations = resolve_script_generation_duration(
            max_duration_seconds, service
        )
        request = ScriptGenerationRequest(
            product=payload["product"],
            image_url=payload.get("image_url"),
            reviews=payload.get("reviews") or [],
            custom_prompt=payload.get("prompt"),
            max_duration_seconds=max_duration_seconds,
            channel=payload.get("channel", "Instagram Reels"),
            target_audience=payload.get("target_audience", "육아에 관심 있는 보호자"),
            supported_video_durations=supported_durations,
        )
        script = build_script_client(service).generate_script(request)
        update_job(
            job_id,
            status="COMPLETED",
            stage="COMPLETED",
            script_json=json.dumps(script, ensure_ascii=False),
        )
    except Exception as error:
        logger.warning("script job failed: job_id=%s error=%s", job_id, error)
        update_job(
            job_id,
            status="FAILED",
            stage="SCRIPT_GENERATION",
            error_message=str(error),
        )
    finally:
        if session is not None:
            session.close()


def _build_settings_service() -> tuple[SettingsService | None, Any]:
    from app.core.config import settings
    from app.db import SQLAlchemySettingsRepository, SessionLocal

    if not settings.SETTINGS_ENCRYPTION_KEY:
        return None, None
    session = SessionLocal()
    return SettingsService(SQLAlchemySettingsRepository(session), settings.SETTINGS_ENCRYPTION_KEY), session
