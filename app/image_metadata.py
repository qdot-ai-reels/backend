"""Read metadata from remotely accessible image inputs."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Sequence
from urllib.parse import urlparse


logger = logging.getLogger(__name__)
SUPPORTED_IMAGE_FORMATS = frozenset({"jpeg", "jpg", "png", "bmp", "webp"})


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


def read_image_format(image_url: str) -> str:
    """Return the provider-facing image format using ffprobe."""
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
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                image_url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        image_format = result.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("이미지 형식을 확인할 수 없습니다.") from error

    if not image_format:
        raise ValueError("이미지 형식을 확인할 수 없습니다.")
    return "jpeg" if image_format == "mjpeg" else image_format


def validate_image_format(
    image_url: str,
    *,
    format_reader=read_image_format,
) -> str:
    """Reject image formats unsupported by the video provider."""
    image_format = format_reader(image_url)
    if image_format not in SUPPORTED_IMAGE_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_IMAGE_FORMATS))
        raise ValueError(
            f"이미지 형식 {image_format}은 지원되지 않습니다. "
            f"지원 형식: {supported}"
        )
    return image_format


def validate_image_inputs(
    *,
    image_url: str | None = None,
    influencer_image_url: str | None = None,
    detail_image_urls: Sequence[str] = (),
    dimensions_reader=read_image_dimensions,
    format_reader=read_image_format,
) -> tuple[str, ...]:
    """Validate required images and return only usable detail image URLs.

    Detail images are optional references. A malformed detail image should not
    block generation when the required product and influencer images are valid.
    """
    candidates: list[tuple[str, str]] = []
    if image_url:
        candidates.append(("상품 이미지", image_url))
    if influencer_image_url:
        candidates.append(("AI 인플루언서 이미지", influencer_image_url))
    for _, image_url in candidates:
        validate_image_dimensions(
            image_url,
            dimensions_reader=dimensions_reader,
        )
        if format_reader is not None:
            validate_image_format(image_url, format_reader=format_reader)

    valid_detail_image_urls: list[str] = []
    for index, detail_image_url in enumerate(detail_image_urls, start=1):
        if not detail_image_url:
            continue
        try:
            validate_image_dimensions(
                detail_image_url,
                dimensions_reader=dimensions_reader,
            )
            if format_reader is not None:
                validate_image_format(detail_image_url, format_reader=format_reader)
        except ValueError as error:
            logger.warning(
                "Skipping invalid product detail image: index=%s url=%s reason=%s",
                index,
                detail_image_url,
                error,
            )
            continue
        valid_detail_image_urls.append(detail_image_url)

    return tuple(valid_detail_image_urls)
