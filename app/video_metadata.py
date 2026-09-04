import json
import re
import subprocess
from pathlib import Path

from app.video_validator import VideoMetadata


PRODUCTION_VERTICAL_WIDTH = 1080
PRODUCTION_VERTICAL_HEIGHT = 1920
PRODUCTION_SQUARE_MIN_EDGE = 1080
PRODUCTION_SQUARE_ASPECT_TOLERANCE = 0.05


def parse_ffprobe_output(output: str) -> VideoMetadata:
    payload = json.loads(output)
    video_stream = next(
        (
            stream
            for stream in payload.get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        None,
    )

    if video_stream is None:
        raise ValueError("Video stream was not found")

    try:
        width = int(video_stream["width"])
        height = int(video_stream["height"])
        duration_seconds = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Incomplete video metadata") from error

    fps = _parse_frame_rate(
        video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
    )
    codec = str(video_stream.get("codec_name") or "").strip().lower() or None
    bitrate_value = video_stream.get("bit_rate") or payload.get("format", {}).get("bit_rate")
    try:
        bitrate = int(bitrate_value) if bitrate_value is not None else None
    except (TypeError, ValueError):
        bitrate = None

    return VideoMetadata(
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        fps=fps,
        codec=codec,
        bitrate=bitrate,
    )


def _parse_frame_rate(value: object) -> float | None:
    if not value:
        return None
    text = str(value)
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else None
        return float(text)
    except (TypeError, ValueError):
        return None


def detect_black_frame_ratio(video_path: str | Path, duration_seconds: float) -> float | None:
    """Measure mostly-black intervals with FFmpeg's blackdetect filter."""
    if duration_seconds <= 0:
        return None
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(video_path),
                "-vf",
                "blackdetect=d=0.10:pic_th=0.98",
                "-an",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    durations = [
        float(value)
        for value in re.findall(r"black_duration:([0-9.]+)", result.stderr or "")
    ]
    return min(1.0, sum(durations) / duration_seconds)


def read_video_metadata(video_path: str | Path) -> VideoMetadata:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = parse_ffprobe_output(result.stdout)
    return VideoMetadata(
        width=metadata.width,
        height=metadata.height,
        duration_seconds=metadata.duration_seconds,
        fps=metadata.fps,
        codec=metadata.codec,
        bitrate=metadata.bitrate,
        black_frame_ratio=detect_black_frame_ratio(
            video_path, metadata.duration_seconds
        ),
    )


def is_production_square_source(metadata: VideoMetadata) -> bool:
    """Return whether a source is eligible for the audited square crop fallback."""
    if metadata.width < PRODUCTION_SQUARE_MIN_EDGE:
        return False
    if metadata.height < PRODUCTION_SQUARE_MIN_EDGE:
        return False
    edge_delta_ratio = abs(metadata.width - metadata.height) / max(
        metadata.width, metadata.height
    )
    return edge_delta_ratio <= PRODUCTION_SQUARE_ASPECT_TOLERANCE


def center_crop_square_video_to_vertical(
    source_path: str | Path,
    destination_path: str | Path,
    metadata: VideoMetadata | None = None,
) -> None:
    """Center-crop an audited high-resolution square MP4 to 1080x1920.

    This public helper is also the recovery entry point for an already
    downloaded provider MP4. It rejects low-resolution or non-square sources
    before invoking FFmpeg and always strips provider audio.
    """
    source_metadata = metadata or read_video_metadata(source_path)
    if not is_production_square_source(source_metadata):
        raise ValueError(
            "center_crop 정규화는 최소 1080x1080의 near-square 영상만 지원합니다."
        )

    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vf",
            (
                "crop='trunc(ih*9/16/2)*2':ih:'(iw-ow)/2':0,"
                f"scale={PRODUCTION_VERTICAL_WIDTH}:{PRODUCTION_VERTICAL_HEIGHT}:"
                "flags=lanczos,setsar=1,format=yuv420p"
            ),
            "-an",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-b:v",
            "8M",
            "-maxrate",
            "10M",
            "-bufsize",
            "16M",
            "-movflags",
            "+faststart",
            str(destination_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def pad_video_to_vertical_canvas(
    source_path: str | Path,
    destination_path: str | Path,
    metadata: VideoMetadata,
) -> None:
    """Add black top/bottom padding without stretching the generated video."""
    target_height = round(metadata.width * 16 / 9)
    if target_height % 2:
        target_height += 1

    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vf",
            (
                f"scale={metadata.width}:{target_height}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={metadata.width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(destination_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
