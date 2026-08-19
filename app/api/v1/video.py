import logging
import os
import urllib.request
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.s3 import generate_presigned_url, upload_file_to_s3
from app.script_generator import OpenRouterRequestError
from app.video_generator import (
    VideoGenerationError,
    VideoGenerationRequest,
)
from app.video_validation_pipeline import (
    VideoValidationPipeline,
    VideoValidationPipelineError,
)
from app.api.v1.settings import get_optional_settings_repository
from app.runtime_config import build_video_client, get_video_model_capabilities
from app.settings_service import ProviderCatalogError, SettingsService

logger = logging.getLogger(__name__)
router = APIRouter()


class VideoGenerationBody(BaseModel):
    script: dict[str, Any] = Field(min_length=1)
    image_url: str = Field(min_length=1)
    resolution: str | None = None
    aspect_ratio: str = "9:16"
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
    request = VideoGenerationRequest(
        script=body.script,
        image_url=body.image_url,
        resolution=body.resolution or "1080p",
        aspect_ratio=body.aspect_ratio,
        generate_audio=body.generate_audio,
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
            )
            
        client = build_video_client(service, capabilities)
        max_retries = (
            service.get_runtime_settings().video_generation_retries if service else 1
        )

        # 1회 다운로드 시 영구 temp 폴더에 동시 저장하기 위한 홀더
        captured_file = {"path": None}

        def custom_downloader(url: str, destination: str) -> None:
            """임시 검증 폴더(destination)와 영구 temp 폴더에 영상을 동시 저장"""
            api_key = (
                os.getenv("OPENROUTER_VIDEO_API_KEY")
                or os.getenv("OPENROUTER_API_KEY", "")
            )
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
            with urllib.request.urlopen(req, timeout=120) as response:
                content = response.read()

            # 1) 파이프라인 ffprobe 검증용 파일 저장
            with open(destination, "wb") as f:
                f.write(content)

            # 2) S3 업로드용 영구 temp 파일 저장
            os.makedirs("temp", exist_ok=True)
            permanent_path = "temp/last_generated_reels.mp4"
            with open(permanent_path, "wb") as f:
                f.write(content)
            captured_file["path"] = permanent_path

        # 파이프라인 실행 (검증 + 단일 다운로드)
        result = VideoValidationPipeline(
            generate_video=lambda pipeline_request, _attempt: client.generate_video(
                pipeline_request
            ),
            download_video=custom_downloader,
            max_retries=max_retries,
        ).run(request)

        # -------------------------------------------------------------
        # [S3 업로드 및 Presigned URL 발급]
        # -------------------------------------------------------------
        local_file = captured_file["path"]
        if not local_file or not os.path.exists(local_file):
            raise FileNotFoundError("생성된 로컬 영상 파일을 확보하지 못했습니다.")

        s3_object_key = f"outputs/{result.job_id}.mp4"
        logger.info(f"[S3 Upload] Uploading {local_file} -> s3://{s3_object_key}")
        upload_file_to_s3(local_file, s3_object_key, content_type="video/mp4")

        # 브라우저 재생용 1시간 유효 Presigned URL 생성
        playable_video_url = generate_presigned_url(s3_object_key, expiration=3600)
        logger.info(f"[S3 Presigned URL Generated] {playable_video_url}")

    except ProviderCatalogError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except OpenRouterRequestError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    except (VideoGenerationError, VideoValidationPipelineError) as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    except Exception as error:
        logger.error(f"S3 파이프라인 처리 중 오류: {error}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"영상 S3 처리 실패: {error}",
        ) from error

    return {
        "job_id": result.job_id,
        "status": result.status,
        "video_url": playable_video_url,
        "cost": result.total_cost,
        "attempts": result.attempts,
        "validation": result.validation.checks,
    }


def select_video_resolution(
    service: SettingsService | None,
    capabilities: Any,
) -> str:
    minimum = service.get_runtime_settings().video_min_resolution if service else "720p"
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