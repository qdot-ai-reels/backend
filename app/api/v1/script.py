from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.image_metadata import validate_image_inputs
from app.script_generator import (
    OpenRouterConfigurationError,
    OpenRouterRequestError,
    ScriptGenerationRequest,
    ScriptValidationError,
)
from app.api.v1.settings import get_optional_settings_repository
from app.runtime_config import build_script_client, resolve_script_generation_duration
from app.settings_service import ProviderCatalogError
from app.settings_service import SettingsService


router = APIRouter()


class ScriptGenerationBody(BaseModel):
    product: dict[str, Any] = Field(min_length=1)
    image_url: str | None = None
    reviews: list[Any] = Field(default_factory=list)
    prompt: str | None = None
    max_duration_seconds: int | None = Field(default=None, ge=1, le=30)
    channel: str = "Instagram Reels"
    target_audience: str = "육아에 관심 있는 보호자"


def validate_product_image_inputs(product: dict[str, Any], image_url: str | None) -> None:
    raw_product = product.get("product") if isinstance(product.get("product"), dict) else product
    primary_image_url = image_url or raw_product.get("image_url") or product.get("image_url")
    detail_image_urls = raw_product.get("detail_image_urls", [])
    if not isinstance(detail_image_urls, list):
        detail_image_urls = []
    validate_image_inputs(
        image_url=primary_image_url,
        detail_image_urls=detail_image_urls,
    )


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
    try:
        validate_product_image_inputs(body.product, body.image_url)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    max_duration_seconds = body.max_duration_seconds or (
        service.get_runtime_settings().video_max_duration_seconds if service else 15
    )
    try:
        max_duration_seconds, supported_durations = resolve_script_generation_duration(
            max_duration_seconds, service
        )
        request = ScriptGenerationRequest(
            product=body.product,
            image_url=body.image_url,
            reviews=body.reviews,
            custom_prompt=body.prompt,
            max_duration_seconds=max_duration_seconds,
            channel=body.channel,
            target_audience=body.target_audience,
            supported_video_durations=supported_durations,
        )
        return build_script_client(service).generate_script(request)
    except OpenRouterConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error
    except (OpenRouterRequestError, ScriptValidationError, ProviderCatalogError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error
