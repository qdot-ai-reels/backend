from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VideoMetadata:
    width: int
    height: int
    duration_seconds: float
    fps: float | None = None
    codec: str | None = None
    bitrate: int | None = None
    black_frame_ratio: float | None = None


@dataclass(frozen=True)
class ValidationPolicy:
    expected_duration_seconds: float
    expected_aspect_width: int = 9
    expected_aspect_height: int = 16
    max_width: int = 1080
    max_height: int = 1920
    min_width: int = 0
    min_height: int = 0
    min_fps: float | None = None
    allowed_codecs: tuple[str, ...] = ()
    min_bitrate: int | None = None
    max_black_frame_ratio: float | None = None
    duration_tolerance_seconds: float = 0.1
    aspect_ratio_tolerance: float = 0.01

    @classmethod
    def production(cls, expected_duration_seconds: float) -> "ValidationPolicy":
        return cls(
            expected_duration_seconds=expected_duration_seconds,
            min_width=1080,
            min_height=1920,
            max_width=2160,
            max_height=3840,
            min_fps=24.0,
            allowed_codecs=("h264", "hevc"),
            min_bitrate=2_500_000,
            max_black_frame_ratio=0.03,
            duration_tolerance_seconds=0.25,
        )


@dataclass
class ValidationResult:
    is_valid: bool
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def validate_video(
    metadata: VideoMetadata,
    policy: ValidationPolicy,
) -> ValidationResult:
    expected_aspect_ratio = policy.expected_aspect_width / policy.expected_aspect_height
    actual_aspect_ratio = metadata.width / metadata.height
    aspect_ratio_passed = (
        abs(actual_aspect_ratio - expected_aspect_ratio)
        <= policy.aspect_ratio_tolerance
    )
    resolution_passed = (
        metadata.width >= policy.min_width
        and metadata.height >= policy.min_height
        and
        metadata.width <= policy.max_width
        and metadata.height <= policy.max_height
    )
    duration_passed = abs(
        metadata.duration_seconds - policy.expected_duration_seconds
    ) <= policy.duration_tolerance_seconds

    checks = {
        "aspect_ratio": {
            "passed": aspect_ratio_passed,
            "expected": f"{policy.expected_aspect_width}:{policy.expected_aspect_height}",
            "actual": f"{metadata.width}:{metadata.height}",
        },
        "resolution": {
            "passed": resolution_passed,
            "expected": (
                f"{policy.min_width}x{policy.min_height}~"
                f"{policy.max_width}x{policy.max_height}"
            ),
            "actual": f"{metadata.width}x{metadata.height}",
        },
        "duration": {
            "passed": duration_passed,
            "expected_seconds": policy.expected_duration_seconds,
            "actual_seconds": metadata.duration_seconds,
        },
    }

    if policy.min_fps is not None:
        checks["fps"] = {
            "passed": metadata.fps is not None and metadata.fps >= policy.min_fps,
            "expected": f">= {policy.min_fps}",
            "actual": metadata.fps,
        }
    if policy.allowed_codecs:
        checks["codec"] = {
            "passed": metadata.codec in policy.allowed_codecs,
            "expected": list(policy.allowed_codecs),
            "actual": metadata.codec,
        }
    if policy.min_bitrate is not None:
        checks["bitrate"] = {
            "passed": (
                metadata.bitrate is not None
                and metadata.bitrate >= policy.min_bitrate
            ),
            "expected": f">= {policy.min_bitrate}",
            "actual": metadata.bitrate,
        }
    if policy.max_black_frame_ratio is not None:
        checks["black_frames"] = {
            "passed": (
                metadata.black_frame_ratio is not None
                and metadata.black_frame_ratio <= policy.max_black_frame_ratio
            ),
            "expected": f"<= {policy.max_black_frame_ratio}",
            "actual": metadata.black_frame_ratio,
        }

    errors = [name for name, check in checks.items() if not check["passed"]]
    return ValidationResult(
        is_valid=not errors,
        checks=checks,
        errors=errors,
    )
