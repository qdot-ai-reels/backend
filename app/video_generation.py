from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from app.video_validator import (
    ValidationPolicy,
    ValidationResult,
    VideoMetadata,
    validate_video,
)


@dataclass(frozen=True)
class GenerationInput:
    script: Any
    image_reference: str


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 1

    def __post_init__(self):
        if self.max_retries < 0:
            raise ValueError("max_retries must be zero or greater")


class GenerationStatus(str, Enum):
    COMPLETED = "completed"
    RETRY_EXHAUSTED = "retry_exhausted"


@dataclass(frozen=True)
class GenerationResult:
    status: GenerationStatus
    attempts: int
    validation: ValidationResult


VideoGenerator = Callable[[GenerationInput, int], VideoMetadata]


def generate_with_retry(
    generation_input: GenerationInput,
    validation_policy: ValidationPolicy,
    generate_video: VideoGenerator,
    retry_policy: RetryPolicy,
) -> GenerationResult:
    total_attempts = retry_policy.max_retries + 1
    last_validation: ValidationResult | None = None

    for attempt in range(1, total_attempts + 1):
        metadata = generate_video(generation_input, attempt)
        last_validation = validate_video(metadata, validation_policy)

        if last_validation.is_valid:
            return GenerationResult(
                status=GenerationStatus.COMPLETED,
                attempts=attempt,
                validation=last_validation,
            )

    assert last_validation is not None
    return GenerationResult(
        status=GenerationStatus.RETRY_EXHAUSTED,
        attempts=total_attempts,
        validation=last_validation,
    )
