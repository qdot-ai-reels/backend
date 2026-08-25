"""Create the final captioned MP4 through the HyperFrames container."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.hyperframes_caption import build_composition_html, build_transcript
from app.hyperframes_client import HyperFramesClient, HyperFramesRenderError
from app.video_metadata import read_video_metadata


router = APIRouter()
WORKSPACE = Path(os.getenv("HYPERFRAMES_WORKSPACE", "/var/lib/quedot/hyperframes"))
RUNNER_URL = os.getenv("HYPERFRAMES_RUNNER_URL", "http://hyperframes:8787")


class CaptionRenderBody(BaseModel):
    script: dict[str, Any] = Field(min_length=1)
    video_filename: str = Field(min_length=1)


def _workspace_file(filename: str) -> Path:
    candidate = (WORKSPACE / filename).resolve()
    workspace = WORKSPACE.resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError("공유 작업 디렉터리 밖의 파일은 사용할 수 없습니다.")
    return candidate


@router.post("/caption", status_code=status.HTTP_200_OK, summary="영상에 자막 애니메이션 추가")
def render_captioned_video(body: CaptionRenderBody) -> dict[str, object]:
    project_dir: Path | None = None
    try:
        transcript = build_transcript(body.script)
        source = _workspace_file(body.video_filename)
        if not source.is_file():
            raise ValueError(f"결합된 영상 파일을 찾을 수 없습니다: {body.video_filename}")

        metadata = read_video_metadata(source)
        job_id = uuid.uuid4().hex
        project_dir = WORKSPACE / job_id
        project_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(source, project_dir / "combined.mp4")
        html = build_composition_html(
            "combined.mp4",
            transcript,
            width=metadata.width,
            height=metadata.height,
            duration_seconds=metadata.duration_seconds,
        )
        (project_dir / "index.html").write_text(html, encoding="utf-8")
        result = HyperFramesClient(RUNNER_URL).render(job_id)
    except HyperFramesRenderError as exc:
        if project_dir is not None:
            shutil.rmtree(project_dir, ignore_errors=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        if project_dir is not None:
            shutil.rmtree(project_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "job_id": job_id,
        "status": result["status"],
        "output_filename": f"{job_id}/final.mp4",
        "subtitle_count": len(transcript),
    }


@router.get(
    "/caption/{job_id}/file",
    status_code=status.HTTP_200_OK,
    summary="HyperFrames 결과 영상 조회 또는 다운로드",
)
def get_captioned_video(job_id: str, download: bool = False) -> FileResponse:
    try:
        output = _workspace_file(f"{job_id}/final.mp4")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    if not output.is_file():
        raise HTTPException(status_code=404, detail="HyperFrames 결과 영상을 찾을 수 없습니다.")

    return FileResponse(
        output,
        media_type="video/mp4",
        filename="final.mp4" if download else None,
    )
