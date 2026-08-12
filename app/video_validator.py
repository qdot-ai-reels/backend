from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VideoMetadata:
    width: int
    height: int
    duration_seconds: float


@dataclass(frozen=True)
class ValidationPolicy:
    expected_duration_seconds: float
    expected_aspect_width: int = 9
    expected_aspect_height: int = 16
    max_width: int = 1080
    max_height: int = 1920
    duration_tolerance_seconds: float = 0.1


@dataclass
class ValidationResult:
    is_valid: bool
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def validate_video(
    metadata: VideoMetadata,
    policy: ValidationPolicy,
) -> ValidationResult:
    aspect_ratio_passed = (
        metadata.width * policy.expected_aspect_height
        == metadata.height * policy.expected_aspect_width
    )
    resolution_passed = (
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
            "expected": f"max {policy.max_width}x{policy.max_height}",
            "actual": f"{metadata.width}x{metadata.height}",
        },
        "duration": {
            "passed": duration_passed,
            "expected_seconds": policy.expected_duration_seconds,
            "actual_seconds": metadata.duration_seconds,
        },
    }

    errors = [name for name, check in checks.items() if not check["passed"]]
    return ValidationResult(
        is_valid=not errors,
        checks=checks,
        errors=errors,
    )
