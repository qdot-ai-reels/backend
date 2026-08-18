from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.script_generator import (
    OpenRouterConfigurationError,
    OpenRouterRequestError,
    ScriptGenerationRequest,
    ScriptValidationError,
)
from app.api.v1.settings import get_optional_settings_repository
from app.runtime_config import build_script_client
from app.settings_service import SettingsService


router = APIRouter()


class ScriptGenerationBody(BaseModel):
    product: dict[str, Any] = Field(min_length=1)
    max_duration_seconds: int | None = Field(default=None, ge=1, le=30)
    channel: str = "Instagram Reels"
    target_audience: str = "육아에 관심 있는 보호자"


@router.post(
    "/script",
    status_code=status.HTTP_200_OK,
    summary="공구 상품 정보를 바탕으로 광고 스크립트 생성",
)
def generate_script(
    body: ScriptGenerationBody,
    service: SettingsService | None = Depends(get_optional_settings_repository),
) -> dict[str, Any]:
    if not isinstance(service, SettingsService):
        service = None
    max_duration_seconds = body.max_duration_seconds or (
        service.get_runtime_settings().video_max_duration_seconds if service else 30
    )
    request = ScriptGenerationRequest(
        product=body.product,
        max_duration_seconds=max_duration_seconds,
        channel=body.channel,
        target_audience=body.target_audience,
    )

    try:
        return build_script_client(service).generate_script(request)
    except OpenRouterConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error
    except (OpenRouterRequestError, ScriptValidationError) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error
