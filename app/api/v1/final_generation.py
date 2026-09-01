"""Run script, video, TTS, muxing, and HyperFrames as one job."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field

from app.api.v1.caption import render_captioned_video_file
from app.api.v1.video import publish_validated_video, select_video_resolution
from app.core.config import settings
from app.db import SQLAlchemySettingsRepository, SessionLocal
from app.generation_jobs import create_job, get_job, update_job
from app.image_metadata import validate_image_inputs
from app.media_combiner import combine_video_and_audio
from app.runtime_config import (
    build_script_client,
    build_tts_settings,
    build_video_client,
    get_video_model_capabilities,
    resolve_script_generation_duration,
)
from app.script_generator import ScriptGenerationRequest
from app.settings_service import SettingsService
from app.tts_generator import OpenRouterTTSClient, SceneAudioDurationError
from app.video_generator import VideoGenerationRequest
from app.video_validation_pipeline import VideoValidationPipeline


router = APIRouter()
LOCAL_COMBINED_OUTPUT_DIR = Path(os.getenv("COMBINED_VIDEO_OUTPUT_DIR", "runtime/combined"))
BACKGROUND_VIDEO_MAX_WAIT_SECONDS = 6 * 60
VIDEO_POLL_INTERVAL_SECONDS = 5
BACKGROUND_VIDEO_MAX_POLL_ATTEMPTS = (
    BACKGROUND_VIDEO_MAX_WAIT_SECONDS // VIDEO_POLL_INTERVAL_SECONDS
)
MAX_SCRIPT_REGENERATIONS = 5


class FinalGenerationBody(BaseModel):
    """Accept product context together with the script to be rendered."""

    product: dict[str, Any] = Field(min_length=1)
    script: dict[str, Any] = Field(min_length=1)
    image_url: str | None = Field(default=None, min_length=1)
    influencer_image_url: str = Field(min_length=1)
    reviews: list[Any] = Field(default_factory=list)
    prompt: str | None = None
    max_duration_seconds: int | None = Field(default=None, ge=1, le=30)
    channel: str = "Instagram Reels"
    target_audience: str = "육아에 관심 있는 보호자"


def validate_product_image_inputs(
    product: dict[str, Any],
    image_url: str | None,
    influencer_image_url: str,
) -> None:
    raw_product = product.get("product") if isinstance(product.get("product"), dict) else product
    primary_image_url = image_url or raw_product.get("image_url") or product.get("image_url")
    detail_image_urls = raw_product.get("detail_image_urls", [])
    if not isinstance(detail_image_urls, list):
        detail_image_urls = []
    validate_image_inputs(
        image_url=primary_image_url,
        influencer_image_url=influencer_image_url,
        detail_image_urls=detail_image_urls,
    )

@router.post("/generate", status_code=status.HTTP_202_ACCEPTED, summary="상품 데이터와 스크립트로 전체 릴스 생성 작업 시작")
def start_generation(body: FinalGenerationBody, background_tasks: BackgroundTasks) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    input_type = "product_and_script"
    image_url = body.image_url or _extract_image_url(body.product)
    if not image_url:
        raise HTTPException(status_code=422, detail="상품 이미지 URL이 필요합니다.")
    try:
        validate_product_image_inputs(
            body.product,
            image_url,
            body.influencer_image_url,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    create_job(job_id, input_type=input_type, product=body.product, script=body.script, image_url=image_url)
    background_tasks.add_task(run_generation_job, job_id, body.model_dump())
    return {"job_id": job_id, "status": "PENDING", "status_url": f"/api/v1/reels/generate/{job_id}"}


@router.get("/generate/{job_id}", status_code=status.HTTP_200_OK, summary="전체 릴스 생성 작업 상태 조회")
def get_generation_status(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="생성 작업을 찾을 수 없습니다.")
    if job["status"] == "COMPLETED" and job.get("output_path"):
        job["video_url"] = f"/api/v1/reels/generate/{job_id}/file"
        job["download_url"] = f"/api/v1/reels/generate/{job_id}/file?download=true"
    return job


@router.get("/generate/{job_id}/file", status_code=status.HTTP_200_OK, summary="최종 HyperFrames MP4 조회 또는 다운로드")
def get_generation_file(job_id: str, download: bool = False):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="생성 작업을 찾을 수 없습니다.")
    output_path = job.get("output_path")
    if job["status"] != "COMPLETED" or not output_path:
        raise HTTPException(status_code=409, detail="생성 작업이 아직 완료되지 않았습니다.")
    path = Path(output_path).resolve()
    root = Path(os.getenv("FINAL_OUTPUT_DIR", "runtime/final")).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="최종 영상을 찾을 수 없습니다.")
    from fastapi.responses import FileResponse
    return FileResponse(path, media_type="video/mp4", filename="final.mp4" if download else None)


def run_generation_job(job_id: str, payload: dict[str, Any]) -> None:
    """Execute the long-running pipeline outside the initial HTTP response."""
    update_job(job_id, status="PROCESSING")
    session = None
    try:
        service, session = _build_settings_service()
        script = payload["script"]
        image_url = payload.get("image_url") or _extract_image_url(payload.get("product"))
        influencer_image_url = payload.get("influencer_image_url")
        script, audio_content = _generate_narration_with_script_regeneration(
            payload, script, service
        )
        update_job(job_id, script_json=json.dumps(script, ensure_ascii=False))
        audio_path = Path("runtime/tts") / job_id / "narration.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(audio_content)

        video_result = _generate_video(
            script,
            image_url,
            influencer_image_url,
            _extract_detail_image_urls(payload.get("product")),
            service,
        )
        if not video_result.storage_path:
            raise RuntimeError("검증된 영상의 로컬 저장 경로를 확인할 수 없습니다.")
        update_job(job_id, video_job_id=video_result.job_id, cost=video_result.total_cost)

        combined_path = LOCAL_COMBINED_OUTPUT_DIR / job_id / "combined.mp4"
        combine_video_and_audio(video_result.storage_path, audio_path, combined_path)
        caption_result = render_captioned_video_file(script, combined_path)
        output_path = Path(str(caption_result["output_path"]))
        final_root = Path(os.getenv("FINAL_OUTPUT_DIR", "runtime/final")) / job_id
        final_root.mkdir(parents=True, exist_ok=True)
        final_path = final_root / "final.mp4"
        shutil.copy2(output_path, final_path)
        update_job(job_id, status="COMPLETED", script_json=json.dumps(script, ensure_ascii=False), caption_job_id=str(caption_result["job_id"]), output_path=str(final_path))
    except Exception as error:
        update_job(job_id, status="FAILED", error_message=str(error))
    finally:
        if session is not None:
            session.close()


def _generate_narration_with_script_regeneration(
    payload: dict[str, Any],
    script: dict[str, Any],
    service: SettingsService | None,
) -> tuple[dict[str, Any], bytes]:
    """Validate TTS before video generation and regenerate scripts on overflow."""
    if not isinstance(payload.get("product"), dict):
        raise ValueError("스크립트 재생성에 필요한 상품 데이터가 없습니다.")

    tts_client = OpenRouterTTSClient(
        settings=build_tts_settings(service),
        retry_duration_errors=False,
    )
    current_script = script
    for regeneration in range(MAX_SCRIPT_REGENERATIONS + 1):
        try:
            return current_script, tts_client.generate_narration(current_script)
        except SceneAudioDurationError as error:
            if regeneration >= MAX_SCRIPT_REGENERATIONS:
                raise
            current_script = _generate_script(
                payload,
                service,
                retry_error=error,
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
    max_duration_seconds, supported_durations = resolve_script_generation_duration(
        payload.get("max_duration_seconds")
        or (service.get_runtime_settings().video_max_duration_seconds if service else 15),
        service,
    )
    custom_prompt = payload.get("prompt")
    if retry_error is not None:
        retry_instruction = (
            f"기존 스크립트의 TTS 검증 실패 사유는 다음과 같습니다: {retry_error}. "
            "해당 장면의 voiceover를 줄여 새 스크립트를 생성하세요."
        )
        custom_prompt = "\n".join(
            value for value in (custom_prompt, retry_instruction) if value
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
    )
    return build_script_client(service).generate_script(request)


def _generate_video(
    script: dict[str, Any],
    image_url: str,
    influencer_image_url: str | None,
    detail_image_urls: tuple[str, ...],
    service: SettingsService | None,
):
    capabilities = get_video_model_capabilities(service)
    client = build_video_client(
        service,
        capabilities,
        max_poll_attempts=BACKGROUND_VIDEO_MAX_POLL_ATTEMPTS,
    )
    request = VideoGenerationRequest(
        script=script,
        image_url=image_url,
        resolution=select_video_resolution(service, capabilities),
        aspect_ratio="9:16",
        generate_audio=False,
        influencer_image_url=influencer_image_url,
        detail_image_urls=detail_image_urls,
    )
    retries = service.get_runtime_settings().video_generation_retries if service else 2
    return VideoValidationPipeline(generate_video=lambda pipeline_request, _attempt: client.generate_video(pipeline_request), publish_video=publish_validated_video, max_retries=retries).run(request)


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
