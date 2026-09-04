"""Persistent, recoverable advertising-product catalog for Studio."""

from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import ProductActivationAuditRow, ProductCatalogRow, SessionLocal
from app.image_metadata import validate_image_dimensions, validate_image_inputs


MAX_RAW_PRODUCT_BYTES = 64 * 1024
MAX_DETAIL_IMAGE_URLS = 8
PRODUCT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
BUILTIN_PRODUCT_ID = "c82e2ff2-77a5-4ce8-86f1-09716d197724"
BUILTIN_PRODUCT_IMAGE_URL = (
    "https://shop-phinf.pstatic.net/20250212_206/"
    "1739326199167opdwT_JPEG/578477270082277_556925135.jpg?type=o1000"
)
BUILTIN_PRODUCT_ASSET_AUDITED_AT = datetime.fromisoformat(
    "2026-09-04T04:31:25.855458+00:00"
)
BUILTIN_PRODUCT_REVIEW_NOTE = (
    "기존 representative-unit allowlist 이관; 생성물에서 검증되지 않은 포장 수량 주장 금지"
)


class ProductCatalogError(ValueError):
    """Base error for catalog validation and persistence."""


class ProductCatalogValidationError(ProductCatalogError):
    """Raised when catalog data is not safe or provider-ready."""


class ProductAssetValidationError(ProductCatalogValidationError):
    """Raised when remote pixels fail provider-readiness checks."""


class ProductCatalogConflictError(ProductCatalogError):
    """Raised when a product ID already exists or a write races."""


class ProductCatalogNotFoundError(ProductCatalogError):
    """Raised when a requested product does not exist."""


class ProductCatalogInactiveError(ProductCatalogError):
    """Raised when generation references an inactive or archived product."""


class ProductCatalogRevisionConflictError(ProductCatalogError):
    """Raised when an operator writes against a stale catalog revision."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clean_required(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductCatalogValidationError(f"{label}은 비어 있을 수 없습니다.")
    clean = value.strip()
    if len(clean) > maximum:
        raise ProductCatalogValidationError(f"{label}은 {maximum}자 이하여야 합니다.")
    return clean


def _clean_optional(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProductCatalogValidationError(f"{label}은 문자열이어야 합니다.")
    clean = value.strip()
    if len(clean) > maximum:
        raise ProductCatalogValidationError(f"{label}은 {maximum}자 이하여야 합니다.")
    return clean


def _clean_product_id(value: Any | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    clean = _clean_required(value, label="product_id", maximum=64)
    if not PRODUCT_ID_PATTERN.fullmatch(clean):
        raise ProductCatalogValidationError(
            "product_id는 영문자 또는 숫자로 시작하고 영문자, 숫자, ., _, :, -만 "
            "사용할 수 있습니다."
        )
    return clean


def _clean_detail_urls(value: Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ProductCatalogValidationError("detail_image_urls는 문자열 배열이어야 합니다.")
    if len(value) > MAX_DETAIL_IMAGE_URLS:
        raise ProductCatalogValidationError(
            f"detail_image_urls는 최대 {MAX_DETAIL_IMAGE_URLS}개까지 저장할 수 있습니다."
        )
    cleaned: list[str] = []
    for item in value:
        clean = _clean_required(item, label="상세 이미지 URL", maximum=2048)
        if clean not in cleaned:
            cleaned.append(clean)
    return tuple(cleaned)


def _canonical_json(value: Any, *, label: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise ProductCatalogValidationError(
            f"{label}은 JSON으로 저장할 수 있어야 합니다."
        ) from error
    if len(encoded.encode("utf-8")) > MAX_RAW_PRODUCT_BYTES:
        raise ProductCatalogValidationError(
            f"{label}은 {MAX_RAW_PRODUCT_BYTES} bytes 이하여야 합니다."
        )
    return encoded


def _verify_assets(image_url: str, detail_image_urls: tuple[str, ...]) -> None:
    """Reject every unusable asset instead of silently dropping detail images."""
    try:
        verified_details = validate_image_inputs(
            image_url=image_url,
            detail_image_urls=detail_image_urls,
        )
        if verified_details != detail_image_urls:
            raise ValueError("하나 이상의 상세 이미지를 사용할 수 없습니다.")
        for label, candidate in (
            ("대표 이미지", image_url),
            *(
                (f"상세 이미지 {index}", url)
                for index, url in enumerate(detail_image_urls, start=1)
            ),
        ):
            width, height = validate_image_dimensions(
                candidate,
                minimum_dimension=512,
            )
            if max(width, height) / min(width, height) > 4:
                raise ValueError(
                    f"{label} 비율은 4:1 이하여야 합니다: {width}x{height}."
                )
    except ValueError as error:
        raise ProductAssetValidationError(
            f"상품 이미지를 Production 입력으로 사용할 수 없습니다: {error}"
        ) from error


def _canonical_raw_product(
    source: Mapping[str, Any] | None,
    *,
    product_id: str,
    event_id: str,
    event_name: str,
    curator: str,
    name: str,
    product_option: str,
    sale_price: int,
    discount_label: str,
    image_url: str,
    detail_image_urls: tuple[str, ...],
) -> dict[str, Any]:
    if source is None:
        raw: dict[str, Any] = {}
    elif not isinstance(source, Mapping):
        raise ProductCatalogValidationError("raw_product는 객체여야 합니다.")
    else:
        raw = deepcopy(dict(source))

    # Generation accepts a flat product payload. Remove a nested product object
    # so untrusted raw data cannot shadow the canonical catalog attributes.
    raw.pop("product", None)
    raw.pop("catalog_revision", None)
    raw.update(
        {
            "product_id": product_id,
            "event_id": event_id,
            "event_name": event_name,
            "curator": curator,
            "name": name,
            "option": product_option,
            "sale_price": sale_price,
            "discount_label": discount_label,
            "image_url": image_url,
            "detail_image_urls": list(detail_image_urls),
        }
    )
    _canonical_json(raw, label="raw_product")
    return raw


def _decoded_json(value: str, *, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


def product_to_public(row: ProductCatalogRow) -> dict[str, Any]:
    details = _decoded_json(row.detail_image_urls_json, fallback=[])
    if not isinstance(details, list):
        details = []
    raw_product = _decoded_json(row.raw_product_json, fallback={})
    if not isinstance(raw_product, dict):
        raw_product = {}
    raw_product["catalog_revision"] = row.revision
    created_at = _as_utc(row.created_at)
    updated_at = _as_utc(row.updated_at)
    asset_verified_at = _as_utc(row.asset_verified_at)
    archived_at = _as_utc(row.archived_at)
    return {
        "id": row.product_id,
        "product_id": row.product_id,
        "event_id": row.event_id,
        "event_name": row.event_name,
        "curator": row.curator,
        "name": row.name,
        "option": row.product_option,
        "sale_price": row.sale_price,
        "discount_label": row.discount_label,
        "image_url": row.image_url,
        "detail_image_urls": details,
        "square_output_strategy": row.square_output_strategy,
        "raw_product": raw_product,
        "is_active": bool(row.is_active) and archived_at is None,
        "archived_at": archived_at.isoformat() if archived_at else None,
        "asset_verified_at": (
            asset_verified_at.isoformat() if asset_verified_at else None
        ),
        "activated_at": (
            _as_utc(row.activated_at).isoformat() if row.activated_at else None
        ),
        "activation_review_note": row.activation_review_note,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "revision": row.revision,
    }


def _normalized_create_values(
    values: Mapping[str, Any],
    *,
    verify_assets: bool,
) -> dict[str, Any]:
    product_id = _clean_product_id(values.get("product_id"))
    name = _clean_required(values.get("name"), label="상품명", maximum=255)
    image_url = _clean_required(
        values.get("image_url"), label="대표 이미지 URL", maximum=2048
    )
    event_id = _clean_optional(
        values.get("event_id", ""), label="event_id", maximum=64
    )
    event_name = _clean_optional(
        values.get("event_name", ""), label="행사명", maximum=255
    )
    curator = _clean_optional(
        values.get("curator", ""), label="큐레이터", maximum=255
    )
    product_option = _clean_optional(
        values.get("option", ""), label="상품 옵션", maximum=255
    )
    discount_label = _clean_optional(
        values.get("discount_label", ""), label="할인 문구", maximum=100
    )
    sale_price = values.get("sale_price", 0)
    if (
        isinstance(sale_price, bool)
        or not isinstance(sale_price, int)
        or not 0 <= sale_price <= 2_147_483_647
    ):
        raise ProductCatalogValidationError(
            "sale_price는 0 이상 2147483647 이하의 정수여야 합니다."
        )
    detail_image_urls = _clean_detail_urls(values.get("detail_image_urls", ()))
    strategy = values.get("square_output_strategy", "center_crop")
    if strategy not in {"reject", "center_crop"}:
        raise ProductCatalogValidationError(
            "square_output_strategy는 reject 또는 center_crop이어야 합니다."
        )
    is_active = values.get("is_active", False)
    if not isinstance(is_active, bool):
        raise ProductCatalogValidationError("is_active는 boolean이어야 합니다.")

    if verify_assets:
        _verify_assets(image_url, detail_image_urls)
    raw_product = _canonical_raw_product(
        values.get("raw_product"),
        product_id=product_id,
        event_id=event_id,
        event_name=event_name,
        curator=curator,
        name=name,
        product_option=product_option,
        sale_price=sale_price,
        discount_label=discount_label,
        image_url=image_url,
        detail_image_urls=detail_image_urls,
    )
    return {
        "product_id": product_id,
        "event_id": event_id,
        "event_name": event_name,
        "curator": curator,
        "name": name,
        "product_option": product_option,
        "sale_price": sale_price,
        "discount_label": discount_label,
        "image_url": image_url,
        "detail_image_urls_json": _canonical_json(
            list(detail_image_urls), label="detail_image_urls"
        ),
        "square_output_strategy": strategy,
        "raw_product_json": _canonical_json(raw_product, label="raw_product"),
        "is_active": is_active,
    }


def create_product(values: Mapping[str, Any]) -> dict[str, Any]:
    requested_id = values.get("product_id")
    if requested_id is not None:
        clean_requested_id = _clean_product_id(requested_id)
        with SessionLocal() as session:
            if session.get(ProductCatalogRow, clean_requested_id) is not None:
                raise ProductCatalogConflictError(
                    "같은 product_id의 상품이 이미 존재합니다."
                )
    normalized = _normalized_create_values(values, verify_assets=True)
    if normalized["is_active"]:
        raise ProductCatalogValidationError(
            "새 상품은 비활성 상태로만 등록할 수 있습니다. 이미지와 상품 의미를 "
            "검수한 뒤 활성화 API를 사용해 주세요."
        )
    now = _utc_now()
    row = ProductCatalogRow(
        **normalized,
        archived_at=None,
        asset_verified_at=now,
        activated_at=None,
        activation_review_note=None,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    with SessionLocal() as session:
        session.add(row)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise ProductCatalogConflictError(
                "같은 product_id의 상품이 이미 존재합니다."
            ) from error
        session.refresh(row)
        return product_to_public(row)


def _existing_values(row: ProductCatalogRow) -> dict[str, Any]:
    return {
        "product_id": row.product_id,
        "event_id": row.event_id,
        "event_name": row.event_name,
        "curator": row.curator,
        "name": row.name,
        "option": row.product_option,
        "sale_price": row.sale_price,
        "discount_label": row.discount_label,
        "image_url": row.image_url,
        "detail_image_urls": _decoded_json(
            row.detail_image_urls_json, fallback=[]
        ),
        "square_output_strategy": row.square_output_strategy,
        "raw_product": _decoded_json(row.raw_product_json, fallback={}),
        "is_active": bool(row.is_active),
    }


def update_product(
    product_id: str,
    values: Mapping[str, Any],
    *,
    expected_revision: int,
) -> dict[str, Any]:
    clean_id = _clean_product_id(product_id)
    if not values:
        raise ProductCatalogValidationError("수정할 상품 필드가 필요합니다.")
    if "product_id" in values:
        raise ProductCatalogValidationError("product_id는 수정할 수 없습니다.")
    if "is_active" in values:
        raise ProductCatalogValidationError(
            "is_active는 수정할 수 없습니다. 활성화 또는 비활성화 API를 사용해 주세요."
        )

    # Read the current snapshot first so remote image verification never holds
    # a database row lock. The revision is checked again after verification.
    with SessionLocal() as session:
        row = session.get(ProductCatalogRow, clean_id)
        if row is None:
            raise ProductCatalogNotFoundError("상품을 찾을 수 없습니다.")
        if row.revision != expected_revision:
            raise ProductCatalogRevisionConflictError(
                "다른 작업자가 상품을 변경했습니다. 목록을 새로고침한 뒤 다시 시도해 주세요."
            )

        merged = _existing_values(row)
        prior_image_url = row.image_url
        prior_detail_urls_json = row.detail_image_urls_json

    merged.update(values)
    normalized = _normalized_create_values(merged, verify_assets=False)
    normalized.pop("product_id")
    normalized.pop("is_active")
    assets_changed = (
        normalized["image_url"] != prior_image_url
        or normalized["detail_image_urls_json"] != prior_detail_urls_json
    )
    if assets_changed:
        _verify_assets(
            str(normalized["image_url"]),
            tuple(
                _decoded_json(
                    str(normalized["detail_image_urls_json"]), fallback=[]
                )
            ),
        )

    with SessionLocal() as session:
        row = session.scalar(
            select(ProductCatalogRow)
            .where(ProductCatalogRow.product_id == clean_id)
            .with_for_update()
        )
        if row is None:
            raise ProductCatalogNotFoundError("상품을 찾을 수 없습니다.")
        if row.revision != expected_revision:
            raise ProductCatalogRevisionConflictError(
                "다른 작업자가 상품을 변경했습니다. 목록을 새로고침한 뒤 다시 시도해 주세요."
            )
        for field, value in normalized.items():
            setattr(row, field, value)
        if assets_changed:
            row.asset_verified_at = _utc_now()
        # Any content edit invalidates the prior semantic approval. This keeps
        # an operator's reviewed product from silently changing under Studio.
        row.is_active = False
        row.activated_at = None
        row.activation_review_note = None
        row.revision += 1
        row.updated_at = _utc_now()
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise ProductCatalogConflictError(
                "상품 수정 중 충돌이 발생했습니다. 다시 시도해 주세요."
            ) from error
        session.refresh(row)
        return product_to_public(row)


def list_products(*, include_inactive: bool = False) -> dict[str, Any]:
    with SessionLocal() as session:
        statement = select(ProductCatalogRow)
        if not include_inactive:
            statement = statement.where(
                ProductCatalogRow.is_active.is_(True),
                ProductCatalogRow.archived_at.is_(None),
            )
        rows = session.scalars(
            statement.order_by(
                ProductCatalogRow.is_active.desc(),
                ProductCatalogRow.archived_at.is_(None).desc(),
                ProductCatalogRow.updated_at.desc(),
                ProductCatalogRow.product_id.asc(),
            )
        ).all()
        active_count = int(
            session.scalar(
                select(func.count(ProductCatalogRow.product_id)).where(
                    ProductCatalogRow.is_active.is_(True),
                    ProductCatalogRow.archived_at.is_(None),
                )
            )
            or 0
        )
        return {
            "items": [product_to_public(row) for row in rows],
            "total": len(rows),
            "active_count": active_count,
        }


def _set_product_state(
    product_id: str,
    *,
    is_active: bool,
    archive: bool,
    expected_revision: int,
    review_note: str | None = None,
) -> dict[str, Any]:
    clean_id = _clean_product_id(product_id)
    with SessionLocal() as session:
        row = session.scalar(
            select(ProductCatalogRow)
            .where(ProductCatalogRow.product_id == clean_id)
            .with_for_update()
        )
        if row is None:
            raise ProductCatalogNotFoundError("상품을 찾을 수 없습니다.")
        # A repeated archive is safely idempotent even when the caller still
        # carries the revision from the first successful request.
        if archive and row.archived_at is not None and not row.is_active:
            return product_to_public(row)
        if row.revision != expected_revision:
            raise ProductCatalogRevisionConflictError(
                "다른 작업자가 상품을 변경했습니다. 목록을 새로고침한 뒤 다시 시도해 주세요."
            )
        next_archived_at = _utc_now() if archive else None
        changed = (
            bool(row.is_active) != is_active
            or (row.archived_at is None) != (next_archived_at is None)
        )
        if changed:
            row.is_active = is_active
            row.archived_at = next_archived_at
            row.revision += 1
            row.updated_at = _utc_now()
            if not is_active:
                row.activated_at = None
                row.activation_review_note = None
            elif review_note is not None:
                activated_at = _utc_now()
                row.activated_at = activated_at
                row.activation_review_note = review_note
                session.add(
                    ProductActivationAuditRow(
                        product_id=row.product_id,
                        product_revision=row.revision,
                        review_note=review_note,
                        activated_at=activated_at,
                    )
                )
            session.commit()
            session.refresh(row)
        return product_to_public(row)


def activate_product(
    product_id: str,
    *,
    expected_revision: int,
    asset_review_acknowledged: bool,
    review_note: str,
) -> dict[str, Any]:
    """Activate an inactive product and recover it if it was archived."""
    if not asset_review_acknowledged:
        raise ProductCatalogValidationError(
            "상품 및 이미지의 의미·표시 내용 검수 확인이 필요합니다."
        )
    clean_note = _clean_required(
        review_note, label="활성화 검수 메모", maximum=1000
    )
    return _set_product_state(
        product_id,
        is_active=True,
        archive=False,
        expected_revision=expected_revision,
        review_note=clean_note,
    )


def deactivate_product(product_id: str, *, expected_revision: int) -> dict[str, Any]:
    return _set_product_state(
        product_id,
        is_active=False,
        archive=False,
        expected_revision=expected_revision,
    )


def archive_product(product_id: str, *, expected_revision: int) -> dict[str, Any]:
    """Soft-delete a product so historical generation snapshots remain valid."""
    return _set_product_state(
        product_id,
        is_active=False,
        archive=True,
        expected_revision=expected_revision,
    )


def require_active_product_revision(
    session: Session,
    product_id: str,
    expected_revision: int,
    *,
    lock: bool = False,
) -> ProductCatalogRow:
    """Require the current active catalog revision in the caller's transaction."""
    clean_id = _clean_product_id(product_id)
    statement = select(ProductCatalogRow).where(
        ProductCatalogRow.product_id == clean_id
    )
    if lock:
        statement = statement.with_for_update()
    row = session.scalar(statement)
    if row is None:
        raise ProductCatalogNotFoundError("등록된 상품을 찾을 수 없습니다.")
    if not row.is_active or row.archived_at is not None:
        raise ProductCatalogInactiveError(
            "비활성 또는 보관된 상품은 영상을 생성할 수 없습니다."
        )
    if row.revision != expected_revision:
        raise ProductCatalogRevisionConflictError(
            "상품이 변경되었습니다. 상품 목록을 새로고침한 뒤 새 견적을 사용해 주세요."
        )
    return row


def resolve_active_generation_product(
    submitted_product: Mapping[str, Any],
    submitted_image_url: str | None,
    expected_revision: int,
) -> dict[str, Any]:
    """Resolve an immutable Studio generation snapshot from the server catalog.

    The caller-provided product text is deliberately ignored after identity and
    revision checks. This prevents a client from changing prompts or assets
    while still presenting the request as an approved catalog product.
    """
    nested = submitted_product.get("product")
    source = nested if isinstance(nested, Mapping) else submitted_product
    product_id = source.get("product_id") or submitted_product.get("product_id")
    if product_id is None:
        raise ProductCatalogNotFoundError(
            "product_id가 없습니다. 상품 목록에서 다시 선택해 주세요."
        )
    clean_id = _clean_product_id(product_id)
    embedded_revision = source.get("catalog_revision")
    if embedded_revision is not None and embedded_revision != expected_revision:
        raise ProductCatalogRevisionConflictError(
            "상품 revision 값이 서로 다릅니다. 상품 목록을 새로고침해 주세요."
        )

    with SessionLocal() as session:
        row = require_active_product_revision(
            session,
            clean_id,
            expected_revision,
        )
        if submitted_image_url and submitted_image_url.strip() != row.image_url:
            raise ProductCatalogRevisionConflictError(
                "상품 이미지가 현재 카탈로그와 다릅니다. 상품 목록을 새로고침해 주세요."
            )
        public = product_to_public(row)
        return {
            "product": public["raw_product"],
            "image_url": row.image_url,
            "square_output_strategy": row.square_output_strategy,
            "revision": row.revision,
        }


def seed_builtin_product_catalog(*, bind=None) -> None:
    """Insert the audited bootstrap product once without overriding operator edits."""
    factory = (
        sessionmaker(bind=bind, autoflush=False, autocommit=False)
        if bind is not None
        else SessionLocal
    )
    raw_product = {
        "product_id": BUILTIN_PRODUCT_ID,
        "event_id": "ed53a658-2577-4c66-a190-77d1e007f96c",
        "event_name": "비니맘마 X 착즙하는남자",
        "curator": "비니맘마",
        "name": "착남 사과주스(스파우트) 30포",
        "option": "기본 옵션",
        "sale_price": 22000,
        "discount_label": "60% 할인",
        "image_url": BUILTIN_PRODUCT_IMAGE_URL,
        "detail_image_urls": [],
        "category_group": ["유아 식품"],
        "selling_point": "사과주스를 담은 스파우트 파우치 대표 단품 이미지",
    }
    now = _utc_now()
    with factory() as session:
        if session.get(ProductCatalogRow, BUILTIN_PRODUCT_ID) is not None:
            return
        session.add(
            ProductCatalogRow(
                product_id=BUILTIN_PRODUCT_ID,
                event_id="ed53a658-2577-4c66-a190-77d1e007f96c",
                event_name="비니맘마 X 착즙하는남자",
                curator="비니맘마",
                name="착남 사과주스(스파우트) 30포",
                product_option="기본 옵션",
                sale_price=22000,
                discount_label="60% 할인",
                image_url=BUILTIN_PRODUCT_IMAGE_URL,
                detail_image_urls_json="[]",
                square_output_strategy="center_crop",
                raw_product_json=_canonical_json(raw_product, label="raw_product"),
                is_active=True,
                archived_at=None,
                asset_verified_at=BUILTIN_PRODUCT_ASSET_AUDITED_AT,
                activated_at=now,
                activation_review_note=BUILTIN_PRODUCT_REVIEW_NOTE,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ProductActivationAuditRow(
                product_id=BUILTIN_PRODUCT_ID,
                product_revision=1,
                review_note=BUILTIN_PRODUCT_REVIEW_NOTE,
                activated_at=now,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # Concurrent application startup may insert the same immutable seed.
            session.rollback()
