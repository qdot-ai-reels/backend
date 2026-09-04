import os
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.script_generator import OpenRouterRequestError
from app.media_combiner import remove_audio_track
from app.video_generator import (
    OpenRouterVideoClient,
    VideoGenerationError,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from app.video_validation_pipeline import (
    PublishedVideoArtifact,
    VideoValidationPipeline,
    VideoValidationPipelineError,
)
from app.api.v1.settings import get_optional_settings_repository
from app.runtime_config import build_video_client, get_video_model_capabilities
from app.settings_service import ProviderCatalogError, SettingsService


router = APIRouter()
LOCAL_VIDEO_OUTPUT_DIR = Path(os.getenv("VIDEO_OUTPUT_DIR", "runtime/videos"))


class VideoGenerationBody(BaseModel):
    script: dict[str, Any] = Field(min_length=1)
    image_url: str = Field(min_length=1)
    influencer_image_url: str | None = Field(default=None, min_length=1)
    influencer_image_urls: list[str] = Field(default_factory=list, max_length=2)
    detail_image_urls: list[str] = Field(default_factory=list)
    resolution: str | None = None
    aspect_ratio: Literal["9:16"] = "9:16"
    generate_audio: bool = False


@router.post(
    "/video",
    status_code=status.HTTP_200_OK,
    summary="스크립트와 상품 이미지로 영상 생성",
)
def generate_video(
    body: VideoGenerationBody,
    service: SettingsService | None = Depends(get_optional_settings_repository),
) -> dict[str, Any]:
    if not isinstance(service, SettingsService):
        service = None
    influencer_image_urls = tuple(
        body.influencer_image_urls
        or ([body.influencer_image_url] if body.influencer_image_url else [])
    )
    request = VideoGenerationRequest(
        script=body.script,
        image_url=body.image_url,
        resolution=body.resolution or "1080p",
        aspect_ratio=body.aspect_ratio,
        generate_audio=body.generate_audio,
        influencer_image_url=body.influencer_image_url,
        influencer_image_urls=influencer_image_urls,
        detail_image_urls=tuple(body.detail_image_urls),
    )

    try:
        capabilities = get_video_model_capabilities(service)
        if body.resolution is None:
            resolution = select_video_resolution(service, capabilities)
            request = request.__class__(
                script=request.script,
                image_url=request.image_url,
                resolution=resolution,
                aspect_ratio=request.aspect_ratio,
                generate_audio=request.generate_audio,
                influencer_image_url=request.influencer_image_url,
                influencer_image_urls=request.influencer_image_urls,
                detail_image_urls=request.detail_image_urls,
            )
        client = build_video_client(service, capabilities)
        max_retries = (
            service.get_runtime_settings().video_generation_retries if service else 2
        )
        result = VideoValidationPipeline(
            generate_video=lambda pipeline_request, _attempt: client.generate_video(
                pipeline_request
            ),
            publish_video=publish_validated_video,
            max_retries=max_retries,
            production_mode=True,
        ).run(request)
    except ProviderCatalogError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except OpenRouterRequestError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error
    except (VideoGenerationError, VideoValidationPipelineError) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error

    if result.status != "completed":
        failed_checks = [
            name
            for name, check in result.validation.checks.items()
            if not check.get("passed")
        ]
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "생성된 영상이 필수 검증을 통과하지 못했습니다. "
                f"실패한 검증: {', '.join(failed_checks) or 'unknown'}"
            ),
        )

    return {
        "job_id": result.job_id,
        "status": result.status,
        "video_url": result.video_url,
        "download_url": result.download_url,
        "storage_path": result.storage_path,
        "cost": result.total_cost,
        "attempts": result.attempts,
        "validation": result.validation.checks,
    }


def publish_validated_video(
    video_path: str,
    generated: VideoGenerationResult,
) -> PublishedVideoArtifact:
    """Keep a validated MP4 locally for the current stage-by-stage testing."""
    safe_job_id = re.sub(r"[^A-Za-z0-9._-]", "_", generated.job_id).strip("._") or "job"
    output_path = LOCAL_VIDEO_OUTPUT_DIR / safe_job_id / "final.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    remove_audio_track(video_path, output_path)
    file_url = f"/api/v1/reels/video/{safe_job_id}/file"
    return PublishedVideoArtifact(
        storage_path=str(output_path),
        playback_url=file_url,
        download_url=f"{file_url}?download=true",
    )


@router.get(
    "/video/{job_id}/url",
    status_code=status.HTTP_200_OK,
    summary="로컬 저장 영상의 재생 또는 다운로드 URL 조회",
)
def get_video_access_url(job_id: str, download: bool = False) -> dict[str, Any]:
    safe_job_id = re.sub(r"[^A-Za-z0-9._-]", "_", job_id).strip("._") or "job"
    url = f"/api/v1/reels/video/{safe_job_id}/file"
    if download:
        url += "?download=true"

    return {
        "job_id": safe_job_id,
        "storage": "local",
        "storage_path": str(LOCAL_VIDEO_OUTPUT_DIR / safe_job_id / "final.mp4"),
        "download": download,
        "url": url,
    }


@router.get(
    "/video/{job_id}/file",
    status_code=status.HTTP_200_OK,
    summary="로컬에 저장된 검증 완료 영상 조회 또는 다운로드",
)
def get_video_file(job_id: str, download: bool = False) -> FileResponse:
    safe_job_id = re.sub(r"[^A-Za-z0-9._-]", "_", job_id).strip("._") or "job"
    path = LOCAL_VIDEO_OUTPUT_DIR / safe_job_id / "final.mp4"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="저장된 영상을 찾을 수 없습니다.")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename="final.mp4" if download else None,
    )


def select_video_resolution(
    service: SettingsService | None,
    capabilities: Any,
) -> str:
    minimum = service.get_runtime_settings().video_min_resolution if service else "1080p"
    maximum = service.get_runtime_settings().video_max_resolution if service else "1080p"

    def pixels(value: str) -> int:
        numeric = value.rstrip("p")
        return int(numeric) if numeric.isdigit() else 0

    candidates = [
        resolution
        for resolution in capabilities.supported_resolutions
        if pixels(minimum) <= pixels(resolution) <= pixels(maximum)
    ]
    if not candidates:
        raise VideoGenerationError(
            f"모델이 설정된 해상도 범위를 지원하지 않습니다: {minimum}~{maximum}"
        )
    return max(candidates, key=pixels)
