"""Combine an existing video file with a generated narration track."""

from __future__ import annotations

import json
import logging
import math
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
PRODUCTION_LOUDNESS_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"
PRODUCTION_STEREO_FILTER = "aformat=channel_layouts=stereo"
logger = logging.getLogger(__name__)


def _parse_loudness_measurement(output: str) -> dict[str, float] | None:
    """Extract and validate the flat JSON object printed by FFmpeg loudnorm."""
    required = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    bounds = {
        "input_i": (-99.0, 0.0),
        "input_tp": (-99.0, 99.0),
        "input_lra": (0.0, 99.0),
        "input_thresh": (-99.0, 0.0),
        "target_offset": (-99.0, 99.0),
    }
    decoder = json.JSONDecoder()
    for index in range(len(output) - 1, -1, -1):
        if output[index] != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or any(key not in payload for key in required):
            continue
        values: dict[str, float] = {}
        try:
            for key in required:
                raw_value = payload[key]
                if isinstance(raw_value, bool):
                    raise ValueError
                value = float(raw_value)
                minimum, maximum = bounds[key]
                if not math.isfinite(value) or not minimum <= value <= maximum:
                    raise ValueError
                values[key] = value
        except (TypeError, ValueError):
            continue
        return values
    return None


def _measure_audio_loudness(audio_path: Path) -> dict[str, float] | None:
    """Measure EBU R128 values for the deterministic second loudnorm pass."""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-nostats",
                "-v",
                "info",
                "-i",
                str(audio_path),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-af",
                (
                    f"{PRODUCTION_STEREO_FILTER},"
                    f"{PRODUCTION_LOUDNESS_FILTER}:print_format=json"
                ),
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning(
            "audio loudness measurement unavailable; using safe one-pass fallback: %s",
            type(error).__name__,
        )
        return None
    if result.returncode != 0:
        logger.warning(
            "audio loudness measurement failed; using safe one-pass fallback: returncode=%s",
            result.returncode,
        )
        return None
    measurement = _parse_loudness_measurement(result.stderr or "")
    if measurement is None:
        logger.warning(
            "audio loudness measurement was invalid; using safe one-pass fallback"
        )
    return measurement


def _format_loudness_value(value: float) -> str:
    return format(value, ".12g")


def _production_loudness_filter(measurement: dict[str, float] | None) -> str:
    if measurement is None:
        return PRODUCTION_LOUDNESS_FILTER
    return ":".join(
        (
            PRODUCTION_LOUDNESS_FILTER,
            f"measured_I={_format_loudness_value(measurement['input_i'])}",
            f"measured_TP={_format_loudness_value(measurement['input_tp'])}",
            f"measured_LRA={_format_loudness_value(measurement['input_lra'])}",
            f"measured_thresh={_format_loudness_value(measurement['input_thresh'])}",
            f"offset={_format_loudness_value(measurement['target_offset'])}",
            "linear=true",
        )
    )


def remove_audio_track(video_path: str | Path, output_path: str | Path) -> Path:
    """Copy only the video stream so provider-generated audio is discarded."""
    video = Path(video_path)
    output = Path(output_path)
    if not video.is_file():
        raise MediaCombineError(f"영상 파일을 찾을 수 없습니다: {video}")

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y", "-i", str(video),
                "-map", "0:v:0", "-c:v", "copy", "-an", str(output),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise MediaCombineError("FFmpeg가 설치되지 않았습니다.") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace")[-500:]
        raise MediaCombineError(f"영상 오디오 트랙을 제거하지 못했습니다: {detail}") from error

    return output


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
    """Mux normalized narration into an MP4 without re-encoding the video."""
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

    loudness_filter = _production_loudness_filter(_measure_audio_loudness(audio))
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
        f"[1:a]{PRODUCTION_STEREO_FILTER},{loudness_filter}[audio]",
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
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
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
