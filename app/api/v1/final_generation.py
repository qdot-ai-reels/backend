"""Run script, video, TTS, muxing, and HyperFrames as one job."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.api.v1.caption import render_captioned_video_file
from app.api.v1.video import publish_validated_video, select_video_resolution
from app.core.config import settings
from app.db import SQLAlchemySettingsRepository, SessionLocal
from app.generation_jobs import create_job, get_job, update_job
from app.media_combiner import combine_video_and_audio
from app.runtime_config import build_script_client, build_tts_settings, build_video_client, get_video_model_capabilities
from app.script_generator import ScriptGenerationRequest
from app.settings_service import SettingsService
from app.tts_generator import OpenRouterTTSClient
from app.video_generator import VideoGenerationRequest
from app.video_validation_pipeline import VideoValidationPipeline


router = APIRouter()
LOCAL_COMBINED_OUTPUT_DIR = Path(os.getenv("COMBINED_VIDEO_OUTPUT_DIR", "runtime/combined"))


class FinalGenerationBody(BaseModel):
    """Accept either original product data or an already generated script."""

    product: dict[str, Any] | None = None
    script: dict[str, Any] | None = None
    image_url: str | None = Field(default=None, min_length=1)
    reviews: list[Any] = Field(default_factory=list)
    prompt: str | None = None
    max_duration_seconds: int | None = Field(default=None, ge=1, le=30)
    channel: str = "Instagram Reels"
    target_audience: str = "육아에 관심 있는 보호자"

    @model_validator(mode="after")
    def require_one_input(self) -> "FinalGenerationBody":
        if self.product is None and self.script is None:
            raise ValueError("product 또는 script 중 하나는 필요합니다.")
        if self.product is not None and self.script is not None:
            raise ValueError("product와 script를 동시에 보낼 수 없습니다.")
        return self


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED, summary="상품 JSON 또는 스크립트로 전체 릴스 생성 작업 시작")
def start_generation(body: FinalGenerationBody, background_tasks: BackgroundTasks) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    input_type = "product" if body.product is not None else "script"
    image_url = body.image_url or _extract_image_url(body.product)
    if not image_url:
        raise HTTPException(status_code=422, detail="상품 이미지 URL이 필요합니다.")
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
        script = payload.get("script")
        image_url = payload.get("image_url") or _extract_image_url(payload.get("product"))
        if script is None:
            script = _generate_script(payload, service)
            update_job(job_id, script_json=json.dumps(script, ensure_ascii=False))
        video_result = _generate_video(script, image_url, service)
        if not video_result.storage_path:
            raise RuntimeError("검증된 영상의 로컬 저장 경로를 확인할 수 없습니다.")
        update_job(job_id, video_job_id=video_result.job_id, cost=video_result.total_cost)

        audio_path = Path("runtime/tts") / job_id / "narration.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_content = OpenRouterTTSClient(settings=build_tts_settings(service)).generate_narration(script)
        audio_path.write_bytes(audio_content)
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


def _generate_script(payload: dict[str, Any], service: SettingsService | None) -> dict[str, Any]:
    raw = payload.get("product") or {}
    product = raw.get("product") if isinstance(raw.get("product"), dict) else raw
    request = ScriptGenerationRequest(
        product=product,
        image_url=payload.get("image_url") or _extract_image_url(raw),
        reviews=payload.get("reviews") or raw.get("reviews", []),
        custom_prompt=payload.get("prompt"),
        max_duration_seconds=payload.get("max_duration_seconds") or (service.get_runtime_settings().video_max_duration_seconds if service else 15),
        channel=payload.get("channel", "Instagram Reels"),
        target_audience=payload.get("target_audience", "육아에 관심 있는 보호자"),
    )
    return build_script_client(service).generate_script(request)


def _generate_video(script: dict[str, Any], image_url: str, service: SettingsService | None):
    capabilities = get_video_model_capabilities(service)
    client = build_video_client(service, capabilities)
    request = VideoGenerationRequest(script=script, image_url=image_url, resolution=select_video_resolution(service, capabilities), aspect_ratio="9:16", generate_audio=False)
    retries = service.get_runtime_settings().video_generation_retries if service else 1
    return VideoValidationPipeline(generate_video=lambda pipeline_request, _attempt: client.generate_video(pipeline_request), publish_video=publish_validated_video, max_retries=retries).run(request)


def _extract_image_url(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    product = value.get("product") if isinstance(value.get("product"), dict) else value
    for image_url in (product.get("image_url"), value.get("image_url")):
        if isinstance(image_url, str) and image_url.strip():
            return image_url
    return None


def _build_settings_service() -> tuple[SettingsService | None, Any]:
    if not settings.SETTINGS_ENCRYPTION_KEY:
        return None, None
    session = SessionLocal()
    return SettingsService(SQLAlchemySettingsRepository(session), settings.SETTINGS_ENCRYPTION_KEY), session
