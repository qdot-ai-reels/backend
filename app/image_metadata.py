"""Read metadata from remotely accessible image inputs."""

from __future__ import annotations

import json
import ipaddress
import logging
import os
import socket
import subprocess
import tempfile
from collections.abc import Sequence
from functools import lru_cache
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlparse


logger = logging.getLogger(__name__)
SUPPORTED_IMAGE_FORMATS = frozenset({"jpeg", "jpg", "png", "webp"})
MAX_REMOTE_IMAGE_BYTES = 15 * 1024 * 1024


def _image_host_for_log(image_url: str) -> str:
    """Return only a non-sensitive host label for rejected remote URLs."""
    try:
        return urlparse(image_url).hostname or "<invalid>"
    except ValueError:
        return "<invalid>"


def _host_matches_allowlist(hostname: str, allowed_hosts: Sequence[str]) -> bool:
    for allowed in allowed_hosts:
        allowed = allowed.strip().lower()
        if not allowed:
            continue
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if hostname.endswith(suffix) and hostname != suffix[1:]:
                return True
        elif hostname == allowed:
            return True
    return False


@lru_cache(maxsize=256)
def validate_remote_image_url(image_url: str) -> str:
    """Reject local/private SSRF targets before any server-side image fetch."""
    parsed = urlparse(image_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("이미지 URL은 공개 HTTPS 주소여야 합니다.")
    if parsed.username or parsed.password:
        raise ValueError("이미지 URL에 사용자 인증 정보를 포함할 수 없습니다.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("로컬 또는 사설 네트워크 이미지는 사용할 수 없습니다.")

    allowed_hosts = tuple(
        value.strip()
        for value in os.getenv("ALLOWED_IMAGE_HOSTS", "").split(",")
        if value.strip()
    )
    if allowed_hosts and not _host_matches_allowlist(hostname, allowed_hosts):
        raise ValueError("허용되지 않은 이미지 호스트입니다.")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as error:
        raise ValueError("이미지 호스트 주소를 확인할 수 없습니다.") from error
    if not addresses:
        raise ValueError("이미지 호스트 주소를 확인할 수 없습니다.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as error:
            raise ValueError("이미지 호스트 주소가 올바르지 않습니다.") from error
        if not ip.is_global:
            raise ValueError("로컬 또는 사설 네트워크 이미지는 사용할 수 없습니다.")
    return image_url


class _SafeImageRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_remote_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@lru_cache(maxsize=128)
def _probe_remote_image(image_url: str) -> tuple[int, int, str]:
    safe_url = validate_remote_image_url(image_url)
    request = Request(safe_url, headers={"User-Agent": "QuedotAssetProbe/1.0"})
    opener = build_opener(_SafeImageRedirectHandler())
    try:
        with opener.open(request, timeout=30) as response, tempfile.NamedTemporaryFile(
            suffix=".image"
        ) as output:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_REMOTE_IMAGE_BYTES:
                raise ValueError("이미지 파일이 15MB 제한을 초과합니다.")
            total = 0
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > MAX_REMOTE_IMAGE_BYTES:
                    raise ValueError("이미지 파일이 15MB 제한을 초과합니다.")
                output.write(chunk)
            output.flush()
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,codec_name",
                    "-of",
                    "json",
                    output.name,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
    except ValueError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("이미지를 안전하게 다운로드하거나 확인할 수 없습니다.") from error
    try:
        stream = (json.loads(result.stdout).get("streams") or [])[0]
        return int(stream["width"]), int(stream["height"]), str(stream["codec_name"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("이미지 메타데이터를 확인할 수 없습니다.") from error


def read_image_dimensions(image_url: str) -> tuple[int, int]:
    """Return dimensions after downloading through the SSRF-safe fetcher."""
    width, height, _format = _probe_remote_image(image_url)

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
    """Return the provider-facing format from the safely downloaded image."""
    _width, _height, image_format = _probe_remote_image(image_url)
    image_format = image_format.strip().lower()

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
    influencer_image_urls: Sequence[str] = (),
    detail_image_urls: Sequence[str] = (),
    dimensions_reader=read_image_dimensions,
    format_reader=read_image_format,
    detail_minimum_dimension: int = 240,
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
    candidates.extend(
        (f"AI 인플루언서 이미지 {index}", candidate)
        for index, candidate in enumerate(influencer_image_urls, start=1)
        if candidate
    )
    validate_urls = dimensions_reader is read_image_dimensions
    for _, image_url in candidates:
        if validate_urls:
            validate_remote_image_url(image_url)
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
            if validate_urls:
                validate_remote_image_url(detail_image_url)
            validate_image_dimensions(
                detail_image_url,
                minimum_dimension=detail_minimum_dimension,
                dimensions_reader=dimensions_reader,
            )
            if format_reader is not None:
                validate_image_format(detail_image_url, format_reader=format_reader)
        except ValueError as error:
            logger.warning(
                "Skipping invalid product detail image: index=%s host=%s reason=%s",
                index,
                _image_host_for_log(detail_image_url),
                error,
            )
            continue
        valid_detail_image_urls.append(detail_image_url)

    return tuple(valid_detail_image_urls)


def validate_normalized_influencer_references(
    image_urls: Sequence[str],
    *,
    dimensions_reader=read_image_dimensions,
    minimum_dimension: int = 256,
) -> tuple[str, ...]:
    """Require individual portrait/square identity references, not contact sheets.

    A wide montage is ambiguous to video models because each tile can be treated
    as a different person. Cropping is deliberately a preprocessing concern so
    the generation request never silently changes an identity asset.
    """
    normalized = tuple(dict.fromkeys(url.strip() for url in image_urls if url.strip()))
    if len(normalized) > 2:
        raise ValueError("AI 인플루언서 레퍼런스는 최대 2장까지 사용할 수 있습니다.")
    for index, image_url in enumerate(normalized, start=1):
        width, height = validate_image_dimensions(
            image_url,
            minimum_dimension=minimum_dimension,
            dimensions_reader=dimensions_reader,
        )
        if width / height > 1.1:
            raise ValueError(
                f"AI 인플루언서 레퍼런스 {index}은 한 명만 보이는 세로형 또는 "
                f"정사각형 이미지여야 합니다: {width}x{height}. "
                "콘택트시트는 인물별로 먼저 분리하세요."
            )
    return normalized
