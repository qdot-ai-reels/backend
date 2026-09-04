"""Operator API for persistent advertising products used by Studio."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.products import (
    ProductAssetValidationError,
    ProductCatalogConflictError,
    ProductCatalogNotFoundError,
    ProductCatalogRevisionConflictError,
    ProductCatalogValidationError,
    activate_product,
    archive_product,
    create_product,
    deactivate_product,
    list_products,
    update_product,
)


router = APIRouter()
NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class ProductCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str | None = Field(default=None, min_length=1, max_length=64)
    event_id: str = Field(default="", max_length=64)
    event_name: str = Field(default="", max_length=255)
    curator: str = Field(default="", max_length=255)
    name: str = Field(min_length=1, max_length=255)
    option: str = Field(default="", max_length=255)
    sale_price: int = Field(default=0, ge=0, le=2_147_483_647)
    discount_label: str = Field(default="", max_length=100)
    image_url: str = Field(min_length=1, max_length=2048)
    detail_image_urls: list[str] = Field(default_factory=list, max_length=8)
    square_output_strategy: Literal["reject", "center_crop"] = "center_crop"
    raw_product: dict[str, Any] = Field(default_factory=dict)
    is_active: Literal[False] = False


class ProductUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    event_id: str | None = Field(default=None, max_length=64)
    event_name: str | None = Field(default=None, max_length=255)
    curator: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    option: str | None = Field(default=None, max_length=255)
    sale_price: int | None = Field(default=None, ge=0, le=2_147_483_647)
    discount_label: str | None = Field(default=None, max_length=100)
    image_url: str | None = Field(default=None, min_length=1, max_length=2048)
    detail_image_urls: list[str] | None = Field(default=None, max_length=8)
    square_output_strategy: Literal["reject", "center_crop"] | None = None
    raw_product: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_partial_update(self) -> "ProductUpdateBody":
        supplied = self.model_fields_set - {"expected_revision"}
        if not supplied:
            raise ValueError("수정할 상품 필드가 필요합니다.")
        null_fields = sorted(
            field for field in supplied if getattr(self, field) is None
        )
        if null_fields:
            raise ValueError(
                "상품 필드에 null을 사용할 수 없습니다: " + ", ".join(null_fields)
            )
        return self


class ProductActivationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    asset_review_acknowledged: bool
    review_note: str = Field(min_length=1, max_length=1000)


class ProductStateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


def _catalog_error(error: Exception) -> HTTPException:
    if isinstance(error, ProductCatalogNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"code": "PRODUCT_NOT_FOUND", "message": str(error)},
            headers=NO_STORE_HEADERS,
        )
    if isinstance(error, ProductCatalogRevisionConflictError):
        return HTTPException(
            status_code=409,
            detail={"code": "PRODUCT_REVISION_CONFLICT", "message": str(error)},
            headers=NO_STORE_HEADERS,
        )
    if isinstance(error, ProductCatalogConflictError):
        return HTTPException(
            status_code=409,
            detail={"code": "PRODUCT_ALREADY_EXISTS", "message": str(error)},
            headers=NO_STORE_HEADERS,
        )
    if isinstance(error, ProductAssetValidationError):
        return HTTPException(
            status_code=422,
            detail={"code": "PRODUCT_ASSET_INVALID", "message": str(error)},
            headers=NO_STORE_HEADERS,
        )
    return HTTPException(
        status_code=422,
        detail={"code": "PRODUCT_INVALID", "message": str(error)},
        headers=NO_STORE_HEADERS,
    )


@router.get(
    "/products",
    status_code=status.HTTP_200_OK,
    summary="광고 상품 목록",
)
def get_products(
    response: Response,
    include_inactive: bool = Query(default=False),
) -> dict[str, Any]:
    response.headers.update(NO_STORE_HEADERS)
    return list_products(include_inactive=include_inactive)


@router.post(
    "/products",
    status_code=status.HTTP_201_CREATED,
    summary="광고 상품 등록",
)
def save_product(body: ProductCreateBody, response: Response) -> dict[str, Any]:
    response.headers.update(NO_STORE_HEADERS)
    try:
        return create_product(body.model_dump(mode="python"))
    except ProductCatalogValidationError as error:
        raise _catalog_error(error) from error
    except ProductCatalogConflictError as error:
        raise _catalog_error(error) from error


@router.put(
    "/products/{product_id}",
    status_code=status.HTTP_200_OK,
    summary="광고 상품 수정 후 자동 비활성화",
)
def replace_product(
    body: ProductUpdateBody,
    response: Response,
    product_id: str = Path(min_length=1, max_length=64),
) -> dict[str, Any]:
    response.headers.update(NO_STORE_HEADERS)
    values = body.model_dump(mode="python", exclude_unset=True)
    expected_revision = int(values.pop("expected_revision"))
    try:
        return update_product(
            product_id,
            values,
            expected_revision=expected_revision,
        )
    except (
        ProductCatalogValidationError,
        ProductCatalogConflictError,
        ProductCatalogNotFoundError,
        ProductCatalogRevisionConflictError,
    ) as error:
        raise _catalog_error(error) from error


@router.post(
    "/products/{product_id}/activate",
    status_code=status.HTTP_200_OK,
    summary="상품 의미 검수 확인 후 활성화 또는 보관 복구",
)
def set_product_active(
    body: ProductActivationBody,
    response: Response,
    product_id: str = Path(min_length=1, max_length=64),
) -> dict[str, Any]:
    response.headers.update(NO_STORE_HEADERS)
    try:
        return activate_product(
            product_id,
            expected_revision=body.expected_revision,
            asset_review_acknowledged=body.asset_review_acknowledged,
            review_note=body.review_note,
        )
    except (
        ProductCatalogValidationError,
        ProductCatalogNotFoundError,
        ProductCatalogRevisionConflictError,
    ) as error:
        raise _catalog_error(error) from error


@router.post(
    "/products/{product_id}/deactivate",
    status_code=status.HTTP_200_OK,
    summary="광고 상품 비활성화",
)
def set_product_inactive(
    body: ProductStateBody,
    response: Response,
    product_id: str = Path(min_length=1, max_length=64),
) -> dict[str, Any]:
    response.headers.update(NO_STORE_HEADERS)
    try:
        return deactivate_product(
            product_id,
            expected_revision=body.expected_revision,
        )
    except (
        ProductCatalogValidationError,
        ProductCatalogNotFoundError,
        ProductCatalogRevisionConflictError,
    ) as error:
        raise _catalog_error(error) from error


@router.delete(
    "/products/{product_id}",
    status_code=status.HTTP_200_OK,
    summary="광고 상품 안전 보관(soft archive)",
)
def remove_product(
    response: Response,
    product_id: str = Path(min_length=1, max_length=64),
    expected_revision: int = Query(ge=1),
) -> dict[str, Any]:
    response.headers.update(NO_STORE_HEADERS)
    try:
        return archive_product(
            product_id,
            expected_revision=expected_revision,
        )
    except (
        ProductCatalogValidationError,
        ProductCatalogNotFoundError,
        ProductCatalogRevisionConflictError,
    ) as error:
        raise _catalog_error(error) from error
