from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.script_generator import (
    OpenRouterClient,
    OpenRouterConfigurationError,
    OpenRouterRequestError,
    ScriptGenerationRequest,
    ScriptValidationError,
)


router = APIRouter()


class ScriptGenerationBody(BaseModel):
    product: dict[str, Any] = Field(min_length=1)
    max_duration_seconds: int = Field(default=30, ge=1, le=30)
    channel: str = "Instagram Reels"
    target_audience: str = "육아에 관심 있는 보호자"


@router.post(
    "/script",
    status_code=status.HTTP_200_OK,
    summary="공구 상품 정보를 바탕으로 광고 스크립트 생성",
)
def generate_script(body: ScriptGenerationBody) -> dict[str, Any]:
    request = ScriptGenerationRequest(
        product=body.product,
        max_duration_seconds=body.max_duration_seconds,
        channel=body.channel,
        target_audience=body.target_audience,
    )

    try:
        return OpenRouterClient.from_env().generate_script(request)
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
