"""Combine an existing video file with a generated narration track."""

from __future__ import annotations

import subprocess
from pathlib import Path


class MediaCombineError(RuntimeError):
    """Raised when media metadata cannot be read or media cannot be combined."""


def read_media_duration(path: str | Path) -> float:
    """Read a media file's duration in seconds with FFprobe."""
    media_path = Path(path)
    if not media_path.is_file():
        raise MediaCombineError(f"미디어 파일을 찾을 수 없습니다: {media_path}")

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as error:
        raise MediaCombineError("FFprobe가 설치되지 않았습니다.") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr[-500:]
        raise MediaCombineError(f"미디어 길이를 읽지 못했습니다: {detail}") from error

    try:
        return float(result.stdout.strip())
    except ValueError as error:
        raise MediaCombineError("FFprobe가 올바른 미디어 길이를 반환하지 않았습니다.") from error


def combine_video_and_audio(
    video_path: str | Path,
    audio_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Mux video and narration into an MP4 without re-encoding the video."""
    video = Path(video_path)
    audio = Path(audio_path)
    output = Path(output_path)
    for path, label in ((video, "영상"), (audio, "음성")):
        if not path.is_file():
            raise MediaCombineError(f"{label} 파일을 찾을 수 없습니다: {path}")

    video_duration = read_media_duration(video)
    if video_duration <= 0:
        raise MediaCombineError("영상 길이가 올바르지 않습니다.")

    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-filter_complex",
        "[1:a]apad[audio]",
        "-map",
        "0:v:0",
        "-map",
        "[audio]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-t",
        f"{video_duration:.3f}",
        str(output),
    ]

    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise MediaCombineError("FFmpeg가 설치되지 않았습니다.") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace")[-500:]
        raise MediaCombineError(f"영상과 음성 결합에 실패했습니다: {detail}") from error

    return output
