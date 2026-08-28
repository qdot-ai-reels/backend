"""Read dimensions from remotely accessible image inputs."""

from __future__ import annotations

import json
import subprocess
from urllib.parse import urlparse


def read_image_dimensions(image_url: str) -> tuple[int, int]:
    """Return image width and height using the ffprobe available in Docker."""
    parsed = urlparse(image_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("이미지 URL은 http 또는 https 주소여야 합니다.")

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                image_url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        stream = (json.loads(result.stdout).get("streams") or [])[0]
        width = int(stream["width"])
        height = int(stream["height"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("이미지의 가로·세로 크기를 확인할 수 없습니다.") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("이미지 크기 확인 도구를 실행할 수 없습니다.") from error

    if width < 1 or height < 1:
        raise ValueError("이미지의 가로·세로 크기가 올바르지 않습니다.")
    return width, height


def validate_image_dimensions(
    image_url: str,
    *,
    minimum_dimension: int = 100,
    dimensions_reader=read_image_dimensions,
) -> tuple[int, int]:
    """Reject images whose width or height is smaller than the required minimum."""
    width, height = dimensions_reader(image_url)
    if width < minimum_dimension or height < minimum_dimension:
        raise ValueError(
            f"이미지 크기가 너무 작습니다: {width}x{height}. "
            f"가로와 세로는 각각 {minimum_dimension}px 이상이어야 합니다."
        )
    return width, height
