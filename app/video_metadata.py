import json
import subprocess
from pathlib import Path

from app.video_validator import VideoMetadata


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

    return VideoMetadata(
        width=width,
        height=height,
        duration_seconds=duration_seconds,
    )


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
    return parse_ffprobe_output(result.stdout)


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
