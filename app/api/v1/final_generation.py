"""Run script, video, TTS, muxing, and HyperFrames as one job."""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError

from app.api.v1.caption import render_captioned_video_file
from app.api.v1.video import publish_validated_video, select_video_resolution
from app.core.config import settings
from app.db import SQLAlchemySettingsRepository, SessionLocal
from app.generation_jobs import (
    _error_metadata,
    create_job,
    get_job,
    get_job_idempotency,
    get_job_payload,
    list_generation_jobs,
    update_candidate,
    update_job,
)
from app.generation_quotes import (
    GenerationQuoteError,
    GenerationQuoteExpiredError,
    GenerationQuoteMismatchError,
    QuoteSpec,
    canonical_request_hash,
    create_generation_quote,
    validate_generation_quote,
)
from app.generation_templates import (
    GenerationTemplateError,
    get_generation_template,
    list_generation_templates,
    normalize_generated_script_to_plan,
    validate_script_matches_template,
)
from app.generation_dispatcher import InProcessGenerationDispatcher
from app.image_metadata import (
    validate_image_inputs,
    validate_normalized_influencer_references,
)
from app.media_combiner import combine_video_and_audio
from app.runtime_config import (
    build_script_client,
    build_tts_settings,
    build_video_client,
    get_video_model_capabilities,
    resolve_exact_script_generation_duration,
    resolve_script_generation_duration,
)
from app.script_generator import (
    DEFAULT_SYLLABLES_PER_SECOND,
    ScriptDialogueLengthError,
    ScriptGenerationRequest,
    ScriptValidationError,
    count_speech_syllables,
    normalize_script_subtitles,
    truncate_voiceover_at_boundary,
    validate_script_document,
)
from app.settings_service import SettingsService
from app.tts_generator import OpenRouterTTSClient, SceneAudioDurationError
from app.video_generator import OpenRouterVideoClient, VideoGenerationRequest
from app.video_metadata import read_video_metadata
from app.video_validation_pipeline import (
    PipelineStatus,
    SquareOutputStrategy,
    VideoValidationPipeline,
)
from app.video_validator import ValidationPolicy, ValidationResult, validate_video


router = APIRouter()
logger = logging.getLogger(__name__)
LOCAL_COMBINED_OUTPUT_DIR = Path(os.getenv("COMBINED_VIDEO_OUTPUT_DIR", "runtime/combined"))
# Keep one provider job alive long enough for late completions. The frontend
# polls independently, so this does not block the initial HTTP response.
BACKGROUND_VIDEO_MAX_WAIT_SECONDS = 18 * 60
VIDEO_POLL_INTERVAL_SECONDS = 5
BACKGROUND_VIDEO_MAX_POLL_ATTEMPTS = (
    BACKGROUND_VIDEO_MAX_WAIT_SECONDS // VIDEO_POLL_INTERVAL_SECONDS
)
MAX_SCRIPT_REGENERATIONS = 1


def _script_duration_seconds(script: dict[str, Any]) -> int | None:
    """Recover the initial duration when older clients omit it from the payload."""
    video = script.get("video")
    if not isinstance(video, dict):
        return None
    try:
        duration = int(video.get("video_duration"))
    except (TypeError, ValueError):
        return None
    return duration if 1 <= duration <= 30 else None


class FinalGenerationBody(BaseModel):
    """Accept product context together with the script to be rendered."""

    product: dict[str, Any] = Field(min_length=1)
    script: dict[str, Any] | None = Field(default=None, min_length=1)
    image_url: str | None = Field(default=None, min_length=1)
    influencer_image_url: str | None = Field(default=None, min_length=1)
    influencer_image_urls: list[str] = Field(default_factory=list, max_length=2)
    reviews: list[Any] = Field(default_factory=list)
    prompt: str | None = None
    max_duration_seconds: int | None = Field(default=None, ge=1, le=30)
    channel: str = "Instagram Reels"
    target_audience: str = "육아에 관심 있는 보호자"
    candidate_count: int = Field(default=3, ge=1, le=4)
    template_id: str | None = Field(default=None, min_length=1, max_length=64)
    template_version: int | None = Field(default=None, ge=1)
    quote_id: str | None = Field(default=None, min_length=1, max_length=64)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    resolution: Literal["1080p"] = "1080p"
    visual_mode: Literal[
        "product_only", "model_included", "generated_model"
    ] | None = None
    square_output_strategy: Literal["reject", "center_crop"] = "reject"

    @model_validator(mode="after")
    def validate_workflow_input(self) -> "FinalGenerationBody":
        if self.script is None and self.template_id is None:
            raise ValueError("script 또는 template_id 중 하나가 필요합니다.")
        if self.template_id is None and (
            self.template_version is not None or self.quote_id is not None
        ):
            raise ValueError("template_version과 quote_id는 template_id와 함께 사용해야 합니다.")
        return self


class GenerationQuoteBody(BaseModel):
    template_id: str = Field(min_length=1, max_length=64)
    template_version: int | None = Field(default=None, ge=1)
    candidate_count: int = Field(default=1, ge=1, le=4)
    visual_mode: Literal[
        "product_only", "model_included", "generated_model"
    ] = "generated_model"
    resolution: Literal["1080p"] = "1080p"


@router.get(
    "/generation-templates",
    status_code=status.HTTP_200_OK,
    summary="Studio 영상 전략 템플릿 목록",
)
def get_generation_templates() -> dict[str, Any]:
    templates = [template.to_public() for template in list_generation_templates()]
    return {
        "items": templates,
        "default_template_id": "ugc_full_15",
    }


@router.post(
    "/generation-quotes",
    status_code=status.HTTP_201_CREATED,
    summary="영상 생성 전 비용 견적 저장",
)
def create_quote(body: GenerationQuoteBody) -> dict[str, Any]:
    try:
        template = get_generation_template(body.template_id, body.template_version)
        model_id = _selected_video_model_id()
        return create_generation_quote(
            QuoteSpec(
                template_id=template.template_id,
                template_version=template.version,
                duration_seconds=template.duration_seconds,
                candidate_count=body.candidate_count,
                visual_mode=body.visual_mode,
                resolution=body.resolution,
            ),
            model_id=model_id,
        )
    except (GenerationTemplateError, GenerationQuoteError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get(
    "/generations",
    status_code=status.HTTP_200_OK,
    summary="생성 영상 관리 목록",
)
def get_generations(
    limit: int = Query(default=24, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    job_status: str | None = Query(default=None, alias="status", min_length=1, max_length=32),
) -> dict[str, Any]:
    try:
        return list_generation_jobs(limit=limit, cursor=cursor, status=job_status)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _selected_video_model_id() -> str:
    environment_model = OpenRouterVideoClient.from_env().model
    service = None
    session = None
    try:
        service, session = _build_settings_service()
        if service is None:
            return environment_model
        return service.get_runtime_settings().openrouter_video_model or environment_model
    finally:
        if session is not None:
            session.close()


def validate_product_image_inputs(
    product: dict[str, Any],
    image_url: str | None,
    influencer_image_urls: tuple[str, ...] = (),
) -> None:
    raw_product = product.get("product") if isinstance(product.get("product"), dict) else product
    primary_image_url = image_url or raw_product.get("image_url") or product.get("image_url")
    validate_image_inputs(
        image_url=primary_image_url,
        influencer_image_urls=influencer_image_urls,
    )


def resolve_influencer_image_urls(payload: dict[str, Any]) -> tuple[str, ...]:
    """Resolve explicit normalized refs before an optional environment default."""
    visual_mode = payload.get("visual_mode")
    explicit = payload.get("influencer_image_urls") or []
    legacy = payload.get("influencer_image_url")
    explicit_values = [
        value.strip()
        for value in explicit
        if isinstance(value, str) and value.strip()
    ] if isinstance(explicit, list) else []
    legacy_value = (
        legacy.strip()
        if isinstance(legacy, str) and legacy.strip()
        else None
    )
    if visual_mode in {"product_only", "generated_model"}:
        if explicit_values or legacy_value:
            raise ValueError(
                f"visual_mode={visual_mode} 요청에는 influencer reference를 보낼 수 없습니다."
            )
        return ()
    if explicit_values:
        candidates = explicit_values
    elif legacy_value:
        candidates = [legacy_value]
    else:
        candidates = os.getenv("INFLUENCER_REFERENCE_URLS", "").split(",")
    resolved = tuple(
        dict.fromkeys(
            value.strip()
            for value in candidates
            if isinstance(value, str) and value.strip()
        )
    )
    if visual_mode == "model_included" and not resolved:
        raise ValueError(
            "visual_mode=model_included 요청에는 influencer reference가 필요합니다."
        )
    return resolved

@router.post("/generate", status_code=status.HTTP_202_ACCEPTED, summary="상품 데이터와 스크립트로 전체 릴스 생성 작업 시작")
def start_generation(body: FinalGenerationBody, background_tasks: BackgroundTasks) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    input_type = "product_template" if body.script is None else "product_and_script"
    image_url = body.image_url or _extract_image_url(body.product)
    if not image_url:
        raise HTTPException(status_code=422, detail="상품 이미지 URL이 필요합니다.")
    payload = body.model_dump()
    try:
        template = (
            get_generation_template(body.template_id, body.template_version)
            if body.template_id
            else None
        )
        if template is not None:
            if (
                body.max_duration_seconds is not None
                and body.max_duration_seconds != template.duration_seconds
            ):
                raise GenerationTemplateError(
                    "max_duration_seconds는 선택한 템플릿 길이와 같아야 합니다."
                )
            payload["template"] = template.to_public()
            payload["template_id"] = template.template_id
            payload["template_version"] = template.version
            payload["max_duration_seconds"] = template.duration_seconds
            if body.script is not None:
                supplied_script = validate_script_document(
                    normalize_script_subtitles(body.script),
                    max_duration_seconds=template.duration_seconds,
                )
                payload["script"] = validate_script_matches_template(
                    supplied_script,
                    template,
                )
        influencer_image_urls = resolve_influencer_image_urls(payload)
        effective_visual_mode = payload.get("visual_mode") or (
            "model_included" if influencer_image_urls else "product_only"
        )
        payload["visual_mode"] = effective_visual_mode
        if (
            body.square_output_strategy == SquareOutputStrategy.CENTER_CROP.value
            and influencer_image_urls
        ):
            raise ValueError(
                "center_crop은 검수된 product-only 입력에서만 사용할 수 있습니다."
            )
        validate_product_image_inputs(
            body.product,
            image_url,
            influencer_image_urls,
        )
        validate_normalized_influencer_references(influencer_image_urls)
    except (GenerationTemplateError, ScriptValidationError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    payload["image_url"] = image_url
    payload["influencer_image_urls"] = list(influencer_image_urls)
    payload["influencer_image_url"] = None
    request_payload = dict(payload)
    request_payload.pop("client_request_id", None)
    request_hash = canonical_request_hash(request_payload)

    if body.client_request_id:
        existing = get_job_idempotency(body.client_request_id)
        if existing is not None:
            existing_job_id, existing_hash = existing
            if existing_hash != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="client_request_id가 다른 생성 요청에 이미 사용되었습니다.",
                )
            return _generation_replay_response(
                existing_job_id,
                body.candidate_count,
                payload,
            )

    if template is not None and body.quote_id:
        spec = QuoteSpec(
            template_id=template.template_id,
            template_version=template.version,
            duration_seconds=template.duration_seconds,
            candidate_count=body.candidate_count,
            visual_mode=str(payload["visual_mode"]),
            resolution=body.resolution,
        )
        try:
            payload["quote"] = validate_generation_quote(
                body.quote_id,
                spec,
                model_id=_selected_video_model_id(),
            )
        except GenerationQuoteExpiredError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except GenerationQuoteMismatchError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except GenerationQuoteError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    try:
        create_job(
            job_id,
            input_type=input_type,
            product=body.product,
            script=payload.get("script"),
            image_url=image_url,
            payload=payload,
            candidate_count=body.candidate_count,
            client_request_id=body.client_request_id,
            request_hash=request_hash,
        )
    except IntegrityError as error:
        existing = (
            get_job_idempotency(body.client_request_id)
            if body.client_request_id
            else None
        )
        if existing is None or existing[1] != request_hash:
            raise HTTPException(
                status_code=409,
                detail="동일한 client_request_id의 생성 요청이 이미 존재합니다.",
            ) from error
        return _generation_replay_response(
            existing[0],
            body.candidate_count,
            payload,
        )
    # This is intentionally isolated behind FastAPI's dispatcher boundary. The
    # persisted payload/candidate states allow a production queue worker to call
    # run_generation_job without changing the API contract.
    InProcessGenerationDispatcher(background_tasks).enqueue(
        run_generation_job, job_id, payload
    )
    return _generation_start_response(job_id, body.candidate_count, payload)


def _generation_start_response(
    job_id: str,
    candidate_count: int,
    payload: dict[str, Any],
    *,
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    response = {
        "job_id": job_id,
        "status": "PENDING",
        "stage": "QUEUED",
        "candidate_count": candidate_count,
        "status_url": f"/api/v1/reels/generate/{job_id}",
        "idempotent_replay": idempotent_replay,
    }
    if isinstance(payload.get("template"), dict):
        response["template"] = {
            "id": payload["template"].get("id"),
            "version": payload["template"].get("version"),
            "duration_seconds": payload["template"].get("duration_seconds"),
        }
    if isinstance(payload.get("quote"), dict):
        response["quote"] = {
            "quote_id": payload["quote"].get("quote_id"),
            "currency": payload["quote"].get("currency"),
            "total": payload["quote"].get("total"),
            "coverage": payload["quote"].get("coverage"),
        }
    return response


def _generation_replay_response(
    job_id: str,
    candidate_count: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    job = get_job(job_id)
    replay_payload = dict(payload)
    if job is not None:
        if isinstance(job.get("template"), dict):
            replay_payload["template"] = job["template"]
        if isinstance(job.get("quote"), dict):
            replay_payload["quote"] = job["quote"]
    response = _generation_start_response(
        job_id,
        int((job.get("candidate_count") if job else None) or candidate_count),
        replay_payload,
        idempotent_replay=True,
    )
    if job is not None:
        response["status"] = job.get("status", response["status"])
        response["stage"] = job.get("stage", response["stage"])
    return response


@router.get("/generate/{job_id}", status_code=status.HTTP_200_OK, summary="전체 릴스 생성 작업 상태 조회")
def get_generation_status(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="생성 작업을 찾을 수 없습니다.")
    for candidate in job.get("candidates", []):
        if candidate.get("status") == "COMPLETED" and candidate.get("output_path"):
            candidate_id = candidate["candidate_id"]
            candidate["video_url"] = (
                f"/api/v1/reels/generate/{job_id}/candidates/{candidate_id}/file"
            )
            candidate["download_url"] = candidate["video_url"] + "?download=true"
        candidate.pop("output_path", None)
    if job["status"] in {"COMPLETED", "PARTIAL_COMPLETED"} and job.get("output_path"):
        job["video_url"] = f"/api/v1/reels/generate/{job_id}/file"
        job["download_url"] = f"/api/v1/reels/generate/{job_id}/file?download=true"
    job.pop("output_path", None)
    return job


@router.get("/generate/{job_id}/file", status_code=status.HTTP_200_OK, summary="최종 HyperFrames MP4 조회 또는 다운로드")
def get_generation_file(job_id: str, download: bool = False):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="생성 작업을 찾을 수 없습니다.")
    output_path = job.get("output_path")
    if job["status"] not in {"COMPLETED", "PARTIAL_COMPLETED"} or not output_path:
        raise HTTPException(status_code=409, detail="생성 작업이 아직 완료되지 않았습니다.")
    path = Path(output_path).resolve()
    root = Path(os.getenv("FINAL_OUTPUT_DIR", "runtime/final")).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="최종 영상을 찾을 수 없습니다.")
    from fastapi.responses import FileResponse
    return FileResponse(path, media_type="video/mp4", filename="final.mp4" if download else None)


@router.get(
    "/generate/{job_id}/candidates/{candidate_id}/file",
    status_code=status.HTTP_200_OK,
    summary="영상 후보 MP4 조회 또는 다운로드",
)
def get_candidate_file(job_id: str, candidate_id: str, download: bool = False):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="생성 작업을 찾을 수 없습니다.")
    candidate = next(
        (
            item
            for item in job.get("candidates", [])
            if item.get("candidate_id") == candidate_id
        ),
        None,
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="영상 후보를 찾을 수 없습니다.")
    output_path = candidate.get("output_path")
    if candidate.get("status") != "COMPLETED" or not output_path:
        raise HTTPException(status_code=409, detail="영상 후보가 아직 완료되지 않았습니다.")
    path = Path(output_path).resolve()
    root = Path(os.getenv("FINAL_OUTPUT_DIR", "runtime/final")).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="영상 후보 파일을 찾을 수 없습니다.")
    from fastapi.responses import FileResponse
    filename = f"{candidate_id}.mp4" if download else None
    return FileResponse(path, media_type="video/mp4", filename=filename)


@router.post(
    "/generate/{job_id}/candidates/{candidate_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    summary="실패한 영상 후보만 다시 생성",
)
def retry_candidate(
    job_id: str,
    candidate_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="생성 작업을 찾을 수 없습니다.")
    candidate = next(
        (
            item
            for item in job.get("candidates", [])
            if item.get("candidate_id") == candidate_id
        ),
        None,
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="영상 후보를 찾을 수 없습니다.")
    if candidate.get("status") != "FAILED":
        raise HTTPException(
            status_code=409,
            detail="FAILED 상태의 영상 후보만 다시 생성할 수 있습니다.",
        )
    payload = get_job_payload(job_id)
    if payload is None:
        raise HTTPException(status_code=409, detail="재시도 입력 데이터를 찾을 수 없습니다.")
    audio_path = Path("runtime/tts") / job_id / "narration.mp3"
    if not audio_path.is_file():
        raise HTTPException(status_code=409, detail="재시도용 narration 파일을 찾을 수 없습니다.")
    try:
        update_candidate(
            job_id,
            candidate_id,
            expected_status="FAILED",
            status="PENDING",
            stage="QUEUED",
            provider_job_id=None,
            caption_job_id=None,
            output_path=None,
            validation=None,
            error=None,
            error_code=None,
            retryable=None,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    update_job(job_id, status="PROCESSING", stage="VIDEO_GENERATION", error_message=None)
    InProcessGenerationDispatcher(background_tasks).enqueue(
        run_candidate_retry,
        job_id,
        candidate_id,
        payload,
        float(candidate.get("cost") or 0.0),
        int(candidate.get("attempts") or 0),
    )
    return {
        "job_id": job_id,
        "candidate_id": candidate_id,
        "status": "PENDING",
        "status_url": f"/api/v1/reels/generate/{job_id}",
    }


def run_candidate_retry(
    job_id: str,
    candidate_id: str,
    payload: dict[str, Any],
    prior_cost: float = 0.0,
    prior_attempts: int = 0,
) -> None:
    session = None
    try:
        service, session = _build_settings_service()
        script = payload.get("script")
        image_url = payload.get("image_url") or _extract_image_url(payload.get("product"))
        if not isinstance(script, dict) or not image_url:
            raise ValueError("재시도에 필요한 스크립트 또는 상품 이미지가 없습니다.")
        _run_candidate(
            job_id=job_id,
            candidate_id=candidate_id,
            payload=payload,
            script=script,
            audio_path=Path("runtime/tts") / job_id / "narration.mp3",
            image_url=image_url,
            influencer_image_urls=resolve_influencer_image_urls(payload),
            service=service,
            prior_cost=prior_cost,
            prior_attempts=prior_attempts,
        )
        _finalize_candidate_job(job_id, script)
    except Exception as error:
        logger.warning(
            "candidate retry failed: job_id=%s candidate_id=%s error=%s",
            job_id,
            candidate_id,
            error,
        )
        error_code, retryable = _error_metadata(
            "FAILED", "VIDEO_GENERATION", str(error)
        )
        update_candidate(
            job_id,
            candidate_id,
            status="FAILED",
            stage="VIDEO_GENERATION",
            error=str(error),
            error_code=error_code,
            retryable=retryable,
        )
        payload_script = payload.get("script")
        if isinstance(payload_script, dict):
            _finalize_candidate_job(job_id, payload_script)
    finally:
        if session is not None:
            session.close()


def run_generation_job(job_id: str, payload: dict[str, Any]) -> None:
    """Generate shared narration, then persist each candidate independently."""
    current_stage = (
        "SCRIPT_GENERATION"
        if not isinstance(payload.get("script"), dict)
        else "TTS_GENERATION"
    )
    update_job(job_id, status="PROCESSING", stage=current_stage)

    def set_stage(stage: str) -> None:
        nonlocal current_stage
        current_stage = stage
        logger.warning("generation job stage: job_id=%s stage=%s", job_id, stage)
        update_job(job_id, stage=stage)

    session = None
    try:
        service, session = _build_settings_service()
        script = payload.get("script")
        if not isinstance(script, dict):
            set_stage("SCRIPT_GENERATION")
            script = _generate_script(payload, service)
            payload["script"] = script
            update_job(
                job_id,
                script_json=json.dumps(script, ensure_ascii=False),
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
        image_url = payload.get("image_url") or _extract_image_url(payload.get("product"))
        if not image_url:
            raise ValueError("영상 생성에 필요한 상품 이미지가 없습니다.")
        influencer_image_urls = resolve_influencer_image_urls(payload)
        script, audio_content = _generate_narration_with_script_regeneration(
            payload,
            script,
            service,
            set_stage=set_stage,
        )
        payload["script"] = script
        update_job(
            job_id,
            script_json=json.dumps(script, ensure_ascii=False),
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        audio_path = Path("runtime/tts") / job_id / "narration.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(audio_content)

        candidate_count = int(payload.get("candidate_count") or 1)
        for index in range(1, candidate_count + 1):
            candidate_id = f"candidate-{index:02d}"
            _run_candidate(
                job_id=job_id,
                candidate_id=candidate_id,
                payload=payload,
                script=script,
                audio_path=audio_path,
                image_url=image_url,
                influencer_image_urls=influencer_image_urls,
                service=service,
            )
        _finalize_candidate_job(job_id, script)
    except Exception as error:
        logger.warning(
            "generation job failed: job_id=%s stage=%s error=%s",
            job_id,
            current_stage,
            error,
        )
        _fail_unresolved_candidates(job_id, current_stage, error)
        update_job(
            job_id,
            status="FAILED",
            stage=current_stage,
            error_message=str(error),
        )
    finally:
        if session is not None:
            session.close()


def _fail_unresolved_candidates(job_id: str, stage: str, error: Exception) -> None:
    job = get_job(job_id)
    if job is None:
        return
    error_code, retryable = _error_metadata("FAILED", stage, str(error))
    for candidate in job.get("candidates", []):
        if candidate.get("status") not in {"COMPLETED", "FAILED"}:
            update_candidate(
                job_id,
                candidate["candidate_id"],
                status="FAILED",
                stage=stage,
                error=str(error),
                error_code=error_code,
                retryable=retryable,
            )


def _source_normalization_evidence(video_result: Any) -> dict[str, Any]:
    """Build JSON-safe evidence for a provider-source normalization decision."""
    normalized = getattr(video_result, "source_normalized", False)
    source_metadata = getattr(video_result, "source_metadata", None)
    normalized_metadata = getattr(video_result, "normalized_metadata", None)
    strategy = getattr(video_result, "normalization_strategy", None)

    def numeric(metadata: Any, field: str) -> int | float | None:
        value = getattr(metadata, field, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value

    return {
        "source_normalized": normalized if isinstance(normalized, bool) else False,
        "normalization_strategy": strategy if isinstance(strategy, str) else None,
        "source_width": numeric(source_metadata, "width"),
        "source_height": numeric(source_metadata, "height"),
        "normalized_width": numeric(normalized_metadata, "width"),
        "normalized_height": numeric(normalized_metadata, "height"),
    }


def _run_candidate(
    *,
    job_id: str,
    candidate_id: str,
    payload: dict[str, Any],
    script: dict[str, Any],
    audio_path: Path,
    image_url: str,
    influencer_image_urls: tuple[str, ...],
    service: SettingsService | None,
    prior_cost: float = 0.0,
    prior_attempts: int = 0,
) -> None:
    """Run one paid candidate without failing sibling candidates."""
    stage = "VIDEO_GENERATION"
    update_candidate(
        job_id,
        candidate_id,
        status="PROCESSING",
        stage=stage,
        error=None,
        error_code=None,
        retryable=None,
    )
    update_job(job_id, status="PROCESSING", stage=stage)
    try:
        video_result = _generate_video(
            script,
            image_url,
            influencer_image_urls,
            _extract_detail_image_urls(payload.get("product")),
            service,
            job_id=job_id,
            candidate_id=candidate_id,
            square_output_strategy=str(
                payload.get("square_output_strategy")
                or SquareOutputStrategy.REJECT.value
            ),
            visual_mode=str(payload.get("visual_mode") or "product_only"),
        )
        raw_provider_validation = getattr(video_result, "provider_validation", None)
        if not isinstance(raw_provider_validation, ValidationResult):
            raw_provider_validation = video_result.validation
        provider_validation = raw_provider_validation.checks
        normalized_validation = video_result.validation.checks
        normalization_evidence = _source_normalization_evidence(video_result)
        cumulative_cost = prior_cost + video_result.total_cost
        cumulative_attempts = prior_attempts + video_result.attempts
        update_candidate(
            job_id,
            candidate_id,
            provider_job_id=video_result.job_id,
            attempts=cumulative_attempts,
            cost=cumulative_cost,
            validation={
                "passed": (
                    False
                    if video_result.status != PipelineStatus.COMPLETED
                    else None
                ),
                "checks": normalized_validation,
                "provider_checks": provider_validation,
                "normalized_checks": normalized_validation,
                **normalization_evidence,
            },
        )
        if video_result.status != PipelineStatus.COMPLETED:
            failed = ", ".join(video_result.validation.errors) or "unknown"
            raise RuntimeError(f"영상 후보가 production 검증에 실패했습니다: {failed}")
        if not video_result.storage_path:
            raise RuntimeError("검증된 영상의 로컬 저장 경로를 확인할 수 없습니다.")

        stage = "AUDIO_MERGE"
        update_candidate(job_id, candidate_id, stage=stage)
        update_job(job_id, stage=stage)
        combined_path = LOCAL_COMBINED_OUTPUT_DIR / job_id / candidate_id / "combined.mp4"
        combine_video_and_audio(video_result.storage_path, audio_path, combined_path)

        stage = "CAPTION_RENDER"
        update_candidate(job_id, candidate_id, stage=stage)
        update_job(job_id, stage=stage)
        caption_result = render_captioned_video_file(script, combined_path)
        output_path = Path(str(caption_result["output_path"]))
        expected_duration = float(script["scenes"][-1]["time_range_sec"]["end"])
        final_metadata = read_video_metadata(output_path)
        final_validation = validate_video(
            final_metadata, ValidationPolicy.production(expected_duration)
        )
        validation_payload = {
            "passed": final_validation.is_valid,
            "checks": final_validation.checks,
            "provider_checks": provider_validation,
            "normalized_checks": normalized_validation,
            **normalization_evidence,
            "width": final_metadata.width,
            "height": final_metadata.height,
            "duration_seconds": final_metadata.duration_seconds,
            "fps": final_metadata.fps,
            "codec": final_metadata.codec,
            "bitrate_kbps": (
                round(final_metadata.bitrate / 1000)
                if final_metadata.bitrate is not None
                else None
            ),
            "black_frame_ratio": final_metadata.black_frame_ratio,
            "technical_score": round(
                100
                * sum(check["passed"] for check in final_validation.checks.values())
                / max(1, len(final_validation.checks))
            ),
        }
        update_candidate(
            job_id,
            candidate_id,
            validation=validation_payload,
        )
        if not final_validation.is_valid:
            failed = ", ".join(final_validation.errors) or "unknown"
            raise RuntimeError(f"최종 영상이 production 검증에 실패했습니다: {failed}")
        final_root = Path(os.getenv("FINAL_OUTPUT_DIR", "runtime/final")) / job_id
        final_root.mkdir(parents=True, exist_ok=True)
        final_path = final_root / f"{candidate_id}.mp4"
        shutil.copy2(output_path, final_path)
        update_candidate(
            job_id,
            candidate_id,
            status="COMPLETED",
            stage="COMPLETED",
            provider_job_id=video_result.job_id,
            caption_job_id=str(caption_result["job_id"]),
            output_path=str(final_path),
            attempts=cumulative_attempts,
            cost=cumulative_cost,
            validation=validation_payload,
            error=None,
            error_code=None,
            retryable=False,
        )
    except Exception as error:
        error_code, retryable = _error_metadata("FAILED", stage, str(error))
        logger.warning(
            "generation candidate failed: job_id=%s candidate_id=%s stage=%s error=%s",
            job_id,
            candidate_id,
            stage,
            error,
        )
        update_candidate(
            job_id,
            candidate_id,
            status="FAILED",
            stage=stage,
            error=str(error),
            error_code=error_code,
            retryable=retryable,
        )


def _finalize_candidate_job(job_id: str, script: dict[str, Any]) -> None:
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"작업을 찾을 수 없습니다: {job_id}")
    candidates = job.get("candidates", [])
    completed = [item for item in candidates if item.get("status") == "COMPLETED"]
    failed = [item for item in candidates if item.get("status") == "FAILED"]
    total_cost = sum(float(item.get("cost") or 0.0) for item in candidates)
    if completed and not failed:
        final_status = "COMPLETED"
    elif completed:
        final_status = "PARTIAL_COMPLETED"
    else:
        final_status = "FAILED"
    primary = completed[0] if completed else {}
    error_message = None
    if not completed:
        errors = [str(item.get("error")) for item in failed if item.get("error")]
        error_message = errors[0] if errors else "모든 영상 후보 생성에 실패했습니다."
    update_job(
        job_id,
        status=final_status,
        stage=final_status,
        script_json=json.dumps(script, ensure_ascii=False),
        video_job_id=primary.get("provider_job_id"),
        caption_job_id=primary.get("caption_job_id"),
        output_path=primary.get("output_path"),
        error_message=error_message,
        cost=total_cost,
    )


def _fit_overflowing_scene(
    script: dict[str, Any],
    error: SceneAudioDurationError,
    *,
    force_silence: bool = False,
) -> tuple[dict[str, Any], str]:
    """Shorten only one overflowing scene without inventing new claims."""
    adjusted = normalize_script_subtitles(deepcopy(script))
    scenes = adjusted.get("scenes") or []
    scene_index = error.scene_number - 1
    if not 0 <= scene_index < len(scenes):
        raise ValueError(f"TTS 오류 장면 번호가 올바르지 않습니다: {error.scene_number}")
    auditory = scenes[scene_index].get("auditory") or {}
    voiceover = auditory.get("voiceover")
    if not isinstance(voiceover, str) or not voiceover.strip():
        auditory["voiceover"] = None
        return adjusted, "silenced"

    original = voiceover.strip()
    original_syllables = count_speech_syllables(original)
    if force_silence or original_syllables < 2:
        auditory["voiceover"] = None
        return adjusted, "silenced"

    duration_budget = max(
        1,
        int(error.expected_seconds * DEFAULT_SYLLABLES_PER_SECOND),
    )
    measured_budget = int(
        original_syllables
        * error.expected_seconds
        / max(error.actual_seconds, error.expected_seconds)
        * 0.8
    )
    budget = min(original_syllables - 1, duration_budget, measured_budget)
    shortened = truncate_voiceover_at_boundary(original, budget)
    if not shortened or shortened == original:
        auditory["voiceover"] = None
        return adjusted, "silenced"

    auditory["voiceover"] = shortened
    return adjusted, "shortened"


def _generate_narration_with_deterministic_fallback(
    script: dict[str, Any],
    tts_client: OpenRouterTTSClient,
    first_error: SceneAudioDurationError,
    *,
    set_stage: Any | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Bound TTS corrections to at most two scene-local edits per scene."""
    current_script = normalize_script_subtitles(script)
    scenes = current_script.get("scenes") or []
    last_error = first_error
    correction_counts: dict[int, int] = {}
    max_corrections = max(1, len(scenes) * 2)

    for _correction in range(max_corrections):
        scene_number = last_error.scene_number
        prior_corrections = correction_counts.get(scene_number, 0)
        current_script, action = _fit_overflowing_scene(
            current_script,
            last_error,
            force_silence=prior_corrections >= 1,
        )
        correction_counts[scene_number] = prior_corrections + 1
        logger.warning(
            "deterministic TTS fallback: scene=%s action=%s correction=%s/%s",
            scene_number,
            action,
            _correction + 1,
            max_corrections,
        )
        if set_stage is not None:
            set_stage("TTS_FALLBACK")
        try:
            return current_script, tts_client.generate_narration(current_script)
        except SceneAudioDurationError as error:
            last_error = error

    raise last_error


def _generate_narration_with_script_regeneration(
    payload: dict[str, Any],
    script: dict[str, Any],
    service: SettingsService | None,
    set_stage: Any | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Validate TTS before video generation and regenerate scripts on overflow."""
    if not isinstance(payload.get("product"), dict):
        raise ValueError("스크립트 재생성에 필요한 상품 데이터가 없습니다.")

    tts_client = OpenRouterTTSClient(
        settings=build_tts_settings(service),
        retry_duration_errors=False,
    )
    current_script = normalize_script_subtitles(script)
    regeneration_payload = dict(payload)
    if regeneration_payload.get("max_duration_seconds") is None:
        inferred_duration = _script_duration_seconds(current_script)
        if inferred_duration is not None:
            regeneration_payload["max_duration_seconds"] = inferred_duration
    for regeneration in range(MAX_SCRIPT_REGENERATIONS + 1):
        if set_stage is not None:
            set_stage("TTS_GENERATION")
        try:
            return current_script, tts_client.generate_narration(current_script)
        except SceneAudioDurationError as error:
            if regeneration >= MAX_SCRIPT_REGENERATIONS:
                return _generate_narration_with_deterministic_fallback(
                    current_script,
                    tts_client,
                    error,
                    set_stage=set_stage,
                )
            if set_stage is not None:
                set_stage("SCRIPT_REGENERATION")
            try:
                current_script = normalize_script_subtitles(
                    _generate_script(
                        regeneration_payload,
                        service,
                        retry_error=error,
                    )
                )
            except ScriptDialogueLengthError:
                return _generate_narration_with_deterministic_fallback(
                    current_script,
                    tts_client,
                    error,
                    set_stage=set_stage,
                )

    raise RuntimeError("스크립트 재생성 흐름을 완료하지 못했습니다.")


def _generate_script(
    payload: dict[str, Any],
    service: SettingsService | None,
    *,
    retry_error: Exception | None = None,
) -> dict[str, Any]:
    raw = payload.get("product") or {}
    product = raw.get("product") if isinstance(raw.get("product"), dict) else raw
    template = None
    template_id = payload.get("template_id")
    if isinstance(template_id, str) and template_id:
        template = get_generation_template(template_id, payload.get("template_version"))
        max_duration_seconds, supported_durations = (
            resolve_exact_script_generation_duration(
                template.duration_seconds,
                service,
            )
        )
    else:
        max_duration_seconds, supported_durations = resolve_script_generation_duration(
            payload.get("max_duration_seconds")
            or (service.get_runtime_settings().video_max_duration_seconds if service else 15),
            service,
        )
    custom_prompt = payload.get("prompt")
    retry_instruction = None
    if retry_error is not None:
        retry_instruction = (
            f"기존 스크립트의 TTS 검증 실패 사유는 다음과 같습니다: {retry_error}. "
            "해당 장면의 voiceover를 줄여 새 스크립트를 생성하세요."
        )
    request = ScriptGenerationRequest(
        product=product,
        image_url=payload.get("image_url") or _extract_image_url(raw),
        reviews=payload.get("reviews") or raw.get("reviews", []),
        custom_prompt=custom_prompt,
        max_duration_seconds=max_duration_seconds,
        channel=payload.get("channel", "Instagram Reels"),
        target_audience=payload.get("target_audience", "육아에 관심 있는 보호자"),
        supported_video_durations=supported_durations,
        retry_instruction=retry_instruction,
        template_scene_plan=(template.prompt_scene_plan() if template else None),
    )
    client = build_script_client(service)
    generated = client.generate_script(
        request,
        max_attempts=1 if retry_error is not None else None,
    )
    if template is None:
        return generated
    normalized = normalize_generated_script_to_plan(
        generated,
        template.prompt_scene_plan(),
    )
    validated = validate_script_document(
        normalized,
        max_duration_seconds=template.duration_seconds,
    )
    return validate_script_matches_template(validated, template)


def _generate_video(
    script: dict[str, Any],
    image_url: str,
    influencer_image_url: str | tuple[str, ...] | None,
    detail_image_urls: tuple[str, ...],
    service: SettingsService | None,
    job_id: str | None = None,
    candidate_id: str | None = None,
    square_output_strategy: str = SquareOutputStrategy.REJECT.value,
    visual_mode: str = "product_only",
):
    influencer_image_urls = (
        influencer_image_url
        if isinstance(influencer_image_url, tuple)
        else ((influencer_image_url,) if influencer_image_url else ())
    )
    if (
        square_output_strategy == SquareOutputStrategy.CENTER_CROP.value
        and influencer_image_urls
    ):
        raise ValueError(
            "center_crop은 검수된 product-only 입력에서만 사용할 수 있습니다."
        )
    capabilities = get_video_model_capabilities(service)
    client = build_video_client(
        service,
        capabilities,
        max_poll_attempts=BACKGROUND_VIDEO_MAX_POLL_ATTEMPTS,
        on_submitted=(
            (
                lambda provider_job_id, _polling_url: (
                    update_job(job_id, video_job_id=provider_job_id),
                    update_candidate(
                        job_id,
                        candidate_id,
                        provider_job_id=provider_job_id,
                    )
                    if candidate_id
                    else None,
                )
            )
            if job_id
            else None
        ),
    )
    request = VideoGenerationRequest(
        script=script,
        image_url=image_url,
        resolution=select_video_resolution(service, capabilities),
        aspect_ratio="9:16",
        generate_audio=False,
        influencer_image_urls=influencer_image_urls,
        detail_image_urls=detail_image_urls,
        visual_mode=visual_mode,
    )
    # Candidate diversity replaces invisible provider retries. A failed
    # candidate is retried only through the explicit retry endpoint.
    retries = 0
    return VideoValidationPipeline(
        generate_video=lambda pipeline_request, _attempt: client.generate_video(
            pipeline_request
        ),
        publish_video=publish_validated_video,
        max_retries=retries,
        production_mode=True,
        square_output_strategy=square_output_strategy,
    ).run(request)


def _extract_image_url(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    product = value.get("product") if isinstance(value.get("product"), dict) else value
    for image_url in (product.get("image_url"), value.get("image_url")):
        if isinstance(image_url, str) and image_url.strip():
            return image_url
    return None


def _extract_detail_image_urls(value: Any) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    product = value.get("product") if isinstance(value.get("product"), dict) else value
    candidates = product.get("detail_image_urls", [])
    if not isinstance(candidates, list):
        return ()
    return tuple(
        image_url.strip()
        for image_url in candidates
        if isinstance(image_url, str) and image_url.strip()
    )


def _build_settings_service() -> tuple[SettingsService | None, Any]:
    if not settings.SETTINGS_ENCRYPTION_KEY:
        return None, None
    session = SessionLocal()
    return SettingsService(SQLAlchemySettingsRepository(session), settings.SETTINGS_ENCRYPTION_KEY), session
