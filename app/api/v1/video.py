from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.script_generator import OpenRouterRequestError
from app.video_generator import (
    OpenRouterVideoClient,
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
        result = VideoValidationPipeline(
            generate_video=lambda pipeline_request, _attempt: client.generate_video(
                pipeline_request
            ),
            max_retries=max_retries,
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

    return {
        "job_id": result.job_id,
        "status": result.status,
        "video_url": result.video_url,
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
