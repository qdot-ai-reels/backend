from typing import Any

from fastapi import APIRouter, HTTPException, status
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


router = APIRouter()


class VideoGenerationBody(BaseModel):
    script: dict[str, Any] = Field(min_length=1)
    image_url: str = Field(min_length=1)
    resolution: str = "720p"
    aspect_ratio: str = "9:16"
    generate_audio: bool = False


@router.post(
    "/video",
    status_code=status.HTTP_200_OK,
    summary="스크립트와 상품 이미지로 영상 생성",
)
def generate_video(body: VideoGenerationBody) -> dict[str, Any]:
    request = VideoGenerationRequest(
        script=body.script,
        image_url=body.image_url,
        resolution=body.resolution,
        aspect_ratio=body.aspect_ratio,
        generate_audio=body.generate_audio,
    )

    try:
        client = OpenRouterVideoClient.from_env()
        result = VideoValidationPipeline(
            generate_video=lambda pipeline_request, _attempt: client.generate_video(
                pipeline_request
            ),
        ).run(request)
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
