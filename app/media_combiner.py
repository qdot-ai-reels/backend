"""Combine an existing video file with a generated narration track."""

from __future__ import annotations

import subprocess
from pathlib import Path


class MediaCombineError(RuntimeError):
    """Raised when media metadata cannot be read or media cannot be combined."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "combine_failed",
        retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, object]:
        return {
            "error_type": self.error_type,
            "retryable": self.retryable,
            "details": self.details,
        }


class MediaDurationMismatchError(MediaCombineError):
    """Raised when video and narration durations do not match."""

    def __init__(self, video_seconds: float, audio_seconds: float) -> None:
        super().__init__(
            "영상과 TTS의 재생 시간이 일치하지 않습니다.",
            error_type="duration_mismatch",
            retryable=False,
            details={
                "video_seconds": video_seconds,
                "audio_seconds": audio_seconds,
            },
        )


DEFAULT_DURATION_TOLERANCE_SECONDS = 0.1


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
    duration_tolerance_seconds: float = DEFAULT_DURATION_TOLERANCE_SECONDS,
) -> Path:
    """Mux video and narration into an MP4 without re-encoding the video."""
    if duration_tolerance_seconds < 0:
        raise ValueError("duration_tolerance_seconds는 0 이상이어야 합니다.")

    video = Path(video_path)
    audio = Path(audio_path)
    output = Path(output_path)
    for path, label in ((video, "영상"), (audio, "음성")):
        if not path.is_file():
            raise MediaCombineError(f"{label} 파일을 찾을 수 없습니다: {path}")

    video_duration = read_media_duration(video)
    audio_duration = read_media_duration(audio)
    if video_duration <= 0:
        raise MediaCombineError("영상 길이가 올바르지 않습니다.")
    if audio_duration <= 0:
        raise MediaCombineError("음성 길이가 올바르지 않습니다.")
    if abs(video_duration - audio_duration) > duration_tolerance_seconds:
        raise MediaDurationMismatchError(video_duration, audio_duration)

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
        "[1:a]anull[audio]",
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
        str(output),
    ]

    # PRD currently requires equal input durations and failure on mismatch.
    # Padding/truncation can be reconsidered if the product policy changes.

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
