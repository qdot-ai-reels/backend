"""Immutable, versioned prompt bundles used by Studio generation jobs."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import (
    ActivePromptVersionRow,
    PromptActivationAuditRow,
    PromptVersionRow,
    SessionLocal,
)


PROMPT_TEMPLATE_NAMES = (
    "script_generation",
    "script_tts_repair",
    "video_base",
    "video_identity_reference",
    "video_generated_model",
    "creative_brief",
)
REQUIRED_TEMPLATE_TOKENS = {
    "script_generation": frozenset(
        {"product_context", "creative_brief", "template_scene_plan"}
    ),
    "script_tts_repair": frozenset({"retry_error"}),
    "video_base": frozenset({"script_visual_table"}),
    "video_identity_reference": frozenset(),
    "video_generated_model": frozenset(),
    "creative_brief": frozenset(
        {"advertising_purpose", "cta", "visual_mode"}
    ),
}
ALLOWED_TEMPLATE_TOKENS = {
    "script_generation": frozenset(
        {
            "product_context",
            "creative_brief",
            "template_scene_plan",
            "channel",
            "target_audience",
            "duration_seconds",
            "resolution",
            "aspect_ratio",
            "visual_mode",
            "retry_instruction",
        }
    ),
    "script_tts_repair": frozenset(
        {"retry_error", "duration_seconds", "channel", "target_audience", "visual_mode"}
    ),
    "video_base": frozenset(
        {"script_visual_table", "duration_seconds", "resolution", "aspect_ratio", "visual_mode"}
    ),
    "video_identity_reference": frozenset(
        {"duration_seconds", "resolution", "aspect_ratio", "visual_mode"}
    ),
    "video_generated_model": frozenset(
        {"duration_seconds", "resolution", "aspect_ratio", "visual_mode"}
    ),
    "creative_brief": frozenset(
        {
            "advertising_purpose",
            "cta",
            "visual_mode",
            "must_include",
            "must_exclude",
            "extra_details",
            "channel",
            "target_audience",
            "duration_seconds",
        }
    ),
}
MAX_PROMPT_TEMPLATE_BYTES = 64 * 1024
MAX_PROMPT_BUNDLE_BYTES = 256 * 1024
BUILTIN_PROMPT_BUNDLE_ID = "production-v1"
BUILTIN_PROMPT_BUNDLE_VERSION = 1
BUILTIN_PROMPT_BUNDLE_NAME = "Production v1"
BUILTIN_PROMPT_BUNDLE_DESCRIPTION = (
    "Bundled production prompts seeded from app/prompt_defaults."
)
_PROMPT_DEFAULTS_DIR = Path(__file__).resolve().parent / "prompt_defaults"
_TOKEN_PATTERN = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


class PromptVersionError(ValueError):
    """Base error for prompt bundle validation and lookup."""


class PromptVersionValidationError(PromptVersionError):
    """Raised when a proposed prompt bundle is not safe to persist."""


class PromptVersionConflictError(PromptVersionError):
    """Raised when concurrent immutable version allocation must be retried."""


class PromptVersionNotFoundError(PromptVersionError):
    """Raised when a requested immutable bundle does not exist."""


class ActivePromptVersionMissingError(PromptVersionError):
    """Raised when Studio cannot resolve an explicitly active prompt bundle."""


class PromptRenderError(PromptVersionError):
    """Raised when a validated prompt cannot be rendered deterministically."""


@dataclass(frozen=True)
class PromptBundleSnapshot:
    id: str
    version: int
    name: str
    description: str
    content_sha256: str
    created_at: datetime
    templates: dict[str, str]

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "content_sha256": self.content_sha256,
        }


def _canonical_templates_json(templates: Mapping[str, str]) -> str:
    return json.dumps(
        {name: templates[name] for name in sorted(templates)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def prompt_templates_sha256(templates: Mapping[str, str]) -> str:
    return hashlib.sha256(_canonical_templates_json(templates).encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _tokens_in_template(content: str) -> frozenset[str]:
    return frozenset(_TOKEN_PATTERN.findall(content))


def validate_prompt_templates(templates: Mapping[str, str]) -> dict[str, str]:
    """Validate a complete six-template bundle without normalizing its content."""
    if not isinstance(templates, Mapping):
        raise PromptVersionValidationError("templates는 객체여야 합니다.")
    supplied_names = set(templates)
    expected_names = set(PROMPT_TEMPLATE_NAMES)
    if supplied_names != expected_names:
        missing = sorted(expected_names - supplied_names)
        unknown = sorted(supplied_names - expected_names)
        details = []
        if missing:
            details.append(f"누락: {', '.join(missing)}")
        if unknown:
            details.append(f"알 수 없음: {', '.join(unknown)}")
        raise PromptVersionValidationError(
            "6개 prompt template을 모두 정확히 제공해야 합니다. " + "; ".join(details)
        )

    validated: dict[str, str] = {}
    total_bytes = 0
    for name in PROMPT_TEMPLATE_NAMES:
        content = templates[name]
        if not isinstance(content, str) or not content.strip():
            raise PromptVersionValidationError(f"{name} template은 비어 있을 수 없습니다.")
        content_size = len(content.encode("utf-8"))
        if content_size > MAX_PROMPT_TEMPLATE_BYTES:
            raise PromptVersionValidationError(
                f"{name} template은 {MAX_PROMPT_TEMPLATE_BYTES} bytes 이하여야 합니다."
            )
        total_bytes += content_size

        # Remove valid placeholders once, then reject every remaining double-brace
        # sequence. This catches whitespace, punctuation, unclosed and nested forms.
        without_valid_tokens = _TOKEN_PATTERN.sub("", content)
        if "{{" in without_valid_tokens or "}}" in without_valid_tokens:
            raise PromptVersionValidationError(
                f"{name} template에 잘못된 {{{{token}}}} 문법이 있습니다."
            )
        tokens = _tokens_in_template(content)
        allowed_tokens = ALLOWED_TEMPLATE_TOKENS[name]
        unknown_tokens = sorted(tokens - allowed_tokens)
        if unknown_tokens:
            raise PromptVersionValidationError(
                f"{name} template에 허용되지 않은 token이 있습니다: "
                + ", ".join(unknown_tokens)
            )
        missing_tokens = sorted(REQUIRED_TEMPLATE_TOKENS[name] - tokens)
        if missing_tokens:
            raise PromptVersionValidationError(
                f"{name} template에 필수 token이 없습니다: "
                + ", ".join(missing_tokens)
            )
        validated[name] = content

    if total_bytes > MAX_PROMPT_BUNDLE_BYTES:
        raise PromptVersionValidationError(
            f"prompt bundle은 {MAX_PROMPT_BUNDLE_BYTES} bytes 이하여야 합니다."
        )
    return validated


def load_builtin_prompt_templates() -> dict[str, str]:
    """Load the source-controlled fallback used by seed and legacy low-level APIs."""
    templates: dict[str, str] = {}
    for name in PROMPT_TEMPLATE_NAMES:
        path = _PROMPT_DEFAULTS_DIR / f"{name}.txt"
        try:
            templates[name] = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ActivePromptVersionMissingError(
                f"기본 prompt resource를 읽을 수 없습니다: {name}"
            ) from error
    return validate_prompt_templates(templates)


def builtin_prompt_snapshot() -> PromptBundleSnapshot:
    templates = load_builtin_prompt_templates()
    return PromptBundleSnapshot(
        id=BUILTIN_PROMPT_BUNDLE_ID,
        version=BUILTIN_PROMPT_BUNDLE_VERSION,
        name=BUILTIN_PROMPT_BUNDLE_NAME,
        description=BUILTIN_PROMPT_BUNDLE_DESCRIPTION,
        content_sha256=prompt_templates_sha256(templates),
        created_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        templates=templates,
    )


def render_prompt_template(
    templates: Mapping[str, str],
    template_name: str,
    values: Mapping[str, Any],
) -> str:
    """Render a validated template once; substituted values are never re-evaluated."""
    validated = validate_prompt_templates(templates)
    if template_name not in validated:
        raise PromptRenderError(f"prompt template을 찾을 수 없습니다: {template_name}")
    content = validated[template_name]
    tokens = _tokens_in_template(content)
    missing_values = sorted(token for token in tokens if token not in values)
    if missing_values:
        raise PromptRenderError(
            f"{template_name} prompt render 값이 없습니다: " + ", ".join(missing_values)
        )

    def replace(match: re.Match[str]) -> str:
        value = values[match.group(1)]
        return "" if value is None else str(value)

    # Deliberately do not scan substituted values for token-shaped strings:
    # user data is inserted in one pass and must never become executable syntax.
    return _TOKEN_PATTERN.sub(replace, content)


def render_creative_brief(
    templates: Mapping[str, str],
    *,
    advertising_purpose: str | None,
    cta: str | None,
    visual_mode: str,
    must_include: str | None,
    must_exclude: str | None,
    extra_details: str | None,
    common_values: Mapping[str, Any] | None = None,
) -> str:
    """Render Studio inputs as quoted, explicitly untrusted data."""
    raw_values = {
        "advertising_purpose": advertising_purpose,
        "cta": cta,
        "visual_mode": visual_mode,
        "must_include": must_include,
        "must_exclude": must_exclude,
        "extra_details": extra_details,
    }
    quoted_values = {
        key: json.dumps(value, ensure_ascii=False)
        for key, value in raw_values.items()
    }
    # CTA keeps the legacy human-readable line while remaining JSON-escaped and
    # enclosed by the explicit untrusted-data delimiter below.
    quoted_values["cta"] = json.dumps(cta or "", ensure_ascii=False)[1:-1]
    for key, value in (common_values or {}).items():
        quoted_values.setdefault(key, json.dumps(value, ensure_ascii=False))
    return render_prompt_template(templates, "creative_brief", quoted_values)


def _row_to_snapshot(row: PromptVersionRow) -> PromptBundleSnapshot:
    try:
        templates = json.loads(row.templates_json)
        validated = validate_prompt_templates(templates)
    except (
        json.JSONDecodeError,
        TypeError,
        PromptVersionValidationError,
    ) as error:
        raise ActivePromptVersionMissingError(
            "저장된 prompt bundle을 읽을 수 없습니다."
        ) from error
    content_sha256 = prompt_templates_sha256(validated)
    if content_sha256 != row.content_sha256:
        raise ActivePromptVersionMissingError(
            "저장된 prompt bundle의 무결성 검증에 실패했습니다."
        )
    created_at = _as_utc(row.created_at)
    return PromptBundleSnapshot(
        id=row.bundle_id,
        version=row.version,
        name=row.name,
        description=row.description,
        content_sha256=row.content_sha256,
        created_at=created_at,
        templates=validated,
    )


def create_prompt_version(
    *,
    name: str,
    description: str,
    templates: Mapping[str, str],
) -> PromptBundleSnapshot:
    validated = validate_prompt_templates(templates)
    clean_name = name.strip()
    clean_description = description.strip()
    if not clean_name:
        raise PromptVersionValidationError("prompt version name이 필요합니다.")
    if len(clean_name) > 255:
        raise PromptVersionValidationError("prompt version name은 255자 이하여야 합니다.")
    if len(clean_description) > 2000:
        raise PromptVersionValidationError("prompt version description은 2000자 이하여야 합니다.")

    with SessionLocal() as session:
        next_version = int(session.scalar(select(func.max(PromptVersionRow.version))) or 0) + 1
        row = PromptVersionRow(
            bundle_id=uuid.uuid4().hex,
            version=next_version,
            name=clean_name,
            description=clean_description,
            templates_json=_canonical_templates_json(validated),
            content_sha256=prompt_templates_sha256(validated),
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise PromptVersionConflictError(
                "prompt version을 동시에 저장했습니다. 다시 시도해 주세요."
            ) from error
        session.refresh(row)
        return _row_to_snapshot(row)


def activate_prompt_version(bundle_id: str) -> None:
    """Atomically move the singleton pointer and append its activation audit."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        version = session.get(PromptVersionRow, bundle_id)
        if version is None:
            raise PromptVersionNotFoundError("prompt version을 찾을 수 없습니다.")
        pointer = session.scalar(
            select(ActivePromptVersionRow)
            .where(ActivePromptVersionRow.id == 1)
            .with_for_update()
        )
        if pointer is None:
            pointer = ActivePromptVersionRow(id=1, bundle_id=bundle_id, updated_at=now)
            session.add(pointer)
        else:
            pointer.bundle_id = bundle_id
            pointer.updated_at = now
        session.add(PromptActivationAuditRow(bundle_id=bundle_id, activated_at=now))
        session.commit()


def get_prompt_version(bundle_id: str) -> PromptBundleSnapshot | None:
    with SessionLocal() as session:
        row = session.get(PromptVersionRow, bundle_id)
        if row is None:
            return None
        session.expunge(row)
        return _row_to_snapshot(row)


def get_active_prompt_version() -> PromptBundleSnapshot:
    with SessionLocal() as session:
        row = session.execute(
            select(PromptVersionRow)
            .join(
                ActivePromptVersionRow,
                ActivePromptVersionRow.bundle_id == PromptVersionRow.bundle_id,
            )
            .where(ActivePromptVersionRow.id == 1)
        ).scalar_one_or_none()
        if row is None:
            raise ActivePromptVersionMissingError(
                "활성 prompt version이 없습니다. Studio 설정에서 version을 활성화해 주세요."
            )
        session.expunge(row)
        return _row_to_snapshot(row)


def list_prompt_versions_public() -> dict[str, Any]:
    with SessionLocal() as session:
        pointer = session.get(ActivePromptVersionRow, 1)
        active_bundle_id = pointer.bundle_id if pointer is not None else None
        last_activated = dict(
            session.execute(
                select(
                    PromptActivationAuditRow.bundle_id,
                    func.max(PromptActivationAuditRow.activated_at),
                ).group_by(PromptActivationAuditRow.bundle_id)
            ).all()
        )
        rows = list(
            session.scalars(
                select(PromptVersionRow).order_by(PromptVersionRow.version.desc())
            )
        )
        versions = []
        for row in rows:
            snapshot = _row_to_snapshot(row)
            activated_at = last_activated.get(row.bundle_id)
            versions.append(
                {
                    "id": snapshot.id,
                    "version": snapshot.version,
                    "name": snapshot.name,
                    "description": snapshot.description,
                    "content_sha256": snapshot.content_sha256,
                    "created_at": snapshot.created_at.isoformat(),
                    "activated_at": (
                        _as_utc(activated_at).isoformat()
                        if activated_at is not None
                        else None
                    ),
                    "is_active": snapshot.id == active_bundle_id,
                    "templates": snapshot.templates,
                }
            )
        return {"active_bundle_id": active_bundle_id, "versions": versions}


def seed_builtin_prompt_version(*, bind=None) -> None:
    """Seed and activate v1 exactly once for a newly introduced prompt schema."""
    factory = sessionmaker(bind=bind) if bind is not None else SessionLocal
    templates = load_builtin_prompt_templates()
    now = datetime.now(timezone.utc)
    with factory() as session:
        version_count = int(session.scalar(select(func.count(PromptVersionRow.bundle_id))) or 0)
        if version_count:
            # Never repair a missing/invalid active pointer implicitly. Studio must
            # fail closed until an operator deliberately activates a stored bundle.
            return
        row = PromptVersionRow(
            bundle_id=BUILTIN_PROMPT_BUNDLE_ID,
            version=BUILTIN_PROMPT_BUNDLE_VERSION,
            name=BUILTIN_PROMPT_BUNDLE_NAME,
            description=BUILTIN_PROMPT_BUNDLE_DESCRIPTION,
            templates_json=_canonical_templates_json(templates),
            content_sha256=prompt_templates_sha256(templates),
            created_at=now,
        )
        session.add(row)
        session.add(
            ActivePromptVersionRow(
                id=1,
                bundle_id=BUILTIN_PROMPT_BUNDLE_ID,
                updated_at=now,
            )
        )
        session.add(
            PromptActivationAuditRow(
                bundle_id=BUILTIN_PROMPT_BUNDLE_ID,
                activated_at=now,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # Multiple application workers may bootstrap the same empty schema.
            # The losing insert is successful only when the winner committed the
            # exact bundled row and active pointer; every other collision fails.
            session.rollback()
            seeded = session.get(PromptVersionRow, BUILTIN_PROMPT_BUNDLE_ID)
            pointer = session.get(ActivePromptVersionRow, 1)
            audit_count = int(
                session.scalar(
                    select(func.count(PromptActivationAuditRow.id)).where(
                        PromptActivationAuditRow.bundle_id
                        == BUILTIN_PROMPT_BUNDLE_ID
                    )
                )
                or 0
            )
            if (
                seeded is None
                or pointer is None
                or pointer.bundle_id != BUILTIN_PROMPT_BUNDLE_ID
                or seeded.version != BUILTIN_PROMPT_BUNDLE_VERSION
                or seeded.content_sha256 != prompt_templates_sha256(templates)
                or audit_count < 1
            ):
                raise ActivePromptVersionMissingError(
                    "기본 prompt version을 원자적으로 초기화하지 못했습니다."
                )
