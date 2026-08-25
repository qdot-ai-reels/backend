"""Combine a generated video and narration audio into a final MP4."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from app.media_combiner import MediaCombineError, combine_video_and_audio


combine_router = APIRouter()
LOCAL_COMBINED_OUTPUT_DIR = Path(os.getenv("COMBINED_VIDEO_OUTPUT_DIR", "runtime/combined"))


def _safe_job_id(job_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", job_id).strip("._") or "job"


@combine_router.post(
    "/combine",
    status_code=status.HTTP_200_OK,
    summary="영상 MP4와 TTS MP3를 결합",
)
def combine_media(video: UploadFile = File(...), audio: UploadFile = File(...)) -> dict[str, object]:
    job_id = uuid.uuid4().hex
    output_path = LOCAL_COMBINED_OUTPUT_DIR / job_id / "final.mp4"

    try:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            video_path = workspace / "input.mp4"
            audio_path = workspace / "input.mp3"
            with video_path.open("wb") as target:
                shutil.copyfileobj(video.file, target)
            with audio_path.open("wb") as target:
                shutil.copyfileobj(audio.file, target)
            combine_video_and_audio(video_path, audio_path, output_path)
    except MediaCombineError as error:
        output_path.unlink(missing_ok=True)
        if output_path.parent.exists():
            output_path.parent.rmdir()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.to_dict(),
        ) from error
    except OSError as error:
        output_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"결합 파일을 저장하지 못했습니다: {error}",
        ) from error
    finally:
        video.file.close()
        audio.file.close()

    file_url = f"/api/v1/reels/combine/{job_id}/file"
    return {
        "job_id": job_id,
        "status": "completed",
        "video_url": file_url,
        "download_url": f"{file_url}?download=true",
        "storage_path": str(output_path),
    }


@combine_router.get(
    "/combine/{job_id}/file",
    status_code=status.HTTP_200_OK,
    summary="결합된 MP4 조회 또는 다운로드",
)
def get_combined_video(job_id: str, download: bool = False) -> FileResponse:
    safe_job_id = _safe_job_id(job_id)
    path = LOCAL_COMBINED_OUTPUT_DIR / safe_job_id / "final.mp4"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="결합된 영상을 찾을 수 없습니다.")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename="final.mp4" if download else None,
    )
