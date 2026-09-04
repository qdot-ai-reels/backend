"""Settings-only API for immutable production prompt bundles."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Path, Response, status
from pydantic import BaseModel, Field

from app.prompt_versions import (
    PromptVersionConflictError,
    PromptVersionError,
    PromptVersionNotFoundError,
    PromptVersionValidationError,
    activate_prompt_version,
    create_prompt_version,
    list_prompt_versions_public,
)


router = APIRouter()
NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class PromptVersionCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    templates: dict[str, str]


def _prompt_error(error: Exception, *, status_code: int = 422) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": "PROMPT_TEMPLATE_INVALID", "message": str(error)},
        headers=NO_STORE_HEADERS,
    )


@router.get(
    "/prompt-versions",
    status_code=status.HTTP_200_OK,
    summary="Production prompt version 목록",
)
def get_prompt_versions(response: Response) -> dict[str, Any]:
    response.headers.update(NO_STORE_HEADERS)
    return list_prompt_versions_public()


@router.post(
    "/prompt-versions",
    status_code=status.HTTP_201_CREATED,
    summary="Immutable production prompt version 저장",
)
def save_prompt_version(
    body: PromptVersionCreateBody,
    response: Response,
) -> dict[str, Any]:
    response.headers.update(NO_STORE_HEADERS)
    try:
        created = create_prompt_version(
            name=body.name,
            description=body.description,
            templates=body.templates,
        )
    except PromptVersionConflictError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "PROMPT_VERSION_CONFLICT", "message": str(error)},
            headers=NO_STORE_HEADERS,
        ) from error
    except (PromptVersionValidationError, PromptVersionError) as error:
        raise _prompt_error(error) from error
    return {
        "id": created.id,
        "version": created.version,
        "name": created.name,
        "description": created.description,
        "content_sha256": created.content_sha256,
        "created_at": created.created_at.isoformat(),
        "activated_at": None,
        "is_active": False,
        "templates": created.templates,
    }


@router.post(
    "/prompt-versions/{bundle_id}/activate",
    status_code=status.HTTP_200_OK,
    summary="Production prompt version 활성화",
)
def set_active_prompt_version(
    response: Response,
    bundle_id: str = Path(min_length=1, max_length=64),
) -> dict[str, Any]:
    response.headers.update(NO_STORE_HEADERS)
    try:
        activate_prompt_version(bundle_id)
    except PromptVersionNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "PROMPT_VERSION_NOT_FOUND", "message": str(error)},
            headers=NO_STORE_HEADERS,
        ) from error
    return list_prompt_versions_public()
