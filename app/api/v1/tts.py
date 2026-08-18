from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.tts_generator import (
    OpenRouterTTSClient,
    NarrationValidationError,
    SceneAudioDurationError,
    TTSConfigurationError,
    TTSGenerationError,
)
from app.api.v1.settings import get_optional_settings_repository
from app.runtime_config import build_tts_settings
from app.settings_service import SettingsService


router = APIRouter()


class TTSGenerationBody(BaseModel):
    script: dict[str, Any] = Field(min_length=1)


@router.post(
    "/tts",
    status_code=status.HTTP_200_OK,
    response_class=Response,
    summary="전체 스크립트의 내레이션을 하나의 MP3로 생성",
    responses={200: {"content": {"audio/mpeg": {}}}},
)
def generate_narration(
    body: TTSGenerationBody,
    service: SettingsService | None = Depends(get_optional_settings_repository),
) -> Response:
    if not isinstance(service, SettingsService):
        service = None
    try:
        audio_content = OpenRouterTTSClient(settings=build_tts_settings(service)).generate_narration(
            body.script
        )
    except NarrationValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    except SceneAudioDurationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": str(error),
                "retryable": error.retryable,
                "next_step": error.next_step,
                "scene_number": error.scene_number,
                "expected_seconds": error.expected_seconds,
                "actual_seconds": error.actual_seconds,
            },
        ) from error
    except TTSConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error
    except TTSGenerationError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error

    return Response(
        content=audio_content,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": 'attachment; filename="narration.mp3"',
        },
    )
