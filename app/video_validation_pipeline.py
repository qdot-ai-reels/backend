"""Connect video generation, MP4 metadata validation, and regeneration."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen

from app.core.config import settings as app_settings
from app.video_generator import (
    VideoGenerationRequest,
    VideoGenerationResult,
    VideoGenerationTimeoutError,
)
from app.video_metadata import (
    center_crop_square_video_to_vertical,
    is_production_square_source,
    pad_video_to_vertical_canvas,
    read_video_metadata,
)
from app.video_validator import (
    ValidationPolicy,
    ValidationResult,
    VideoMetadata,
    validate_video,
)


class VideoValidationPipelineError(RuntimeError):
    """Raised when a generated video cannot be downloaded or inspected."""


class PipelineStatus(str, Enum):
    COMPLETED = "completed"
    RETRY_EXHAUSTED = "retry_exhausted"


class SquareOutputStrategy(str, Enum):
    REJECT = "reject"
    CENTER_CROP = "center_crop"


@dataclass(frozen=True)
class PipelineResult:
    status: PipelineStatus
    attempts: int
    job_id: str
    video_url: str
    validation: ValidationResult
    total_cost: float
    storage_path: str | None = None
    download_url: str | None = None
    provider_validation: ValidationResult | None = None
    source_normalized: bool = False
    normalization_strategy: str | None = None
    source_metadata: VideoMetadata | None = None
    normalized_metadata: VideoMetadata | None = None


@dataclass(frozen=True)
class PublishedVideoArtifact:
    storage_path: str
    playback_url: str
    download_url: str


VideoGenerator = Callable[[VideoGenerationRequest, int], VideoGenerationResult]
VideoDownloader = Callable[[str, str], None]
MetadataReader = Callable[[str | Path], Any]
VideoNormalizer = Callable[[str | Path, str | Path, Any], None]
VideoPublisher = Callable[[str | Path, VideoGenerationResult], PublishedVideoArtifact]
logger = logging.getLogger(__name__)
VERTICAL_PADDING_ASPECT_TOLERANCE = 0.05


class VideoValidationPipeline:
    def __init__(
        self,
        generate_video: VideoGenerator,
        download_video: VideoDownloader | None = None,
        read_metadata: MetadataReader = read_video_metadata,
        normalize_video: VideoNormalizer | None = None,
        publish_video: VideoPublisher | None = None,
        max_retries: int = 1,
        production_mode: bool = False,
        square_output_strategy: SquareOutputStrategy | str = SquareOutputStrategy.REJECT,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries는 0 이상이어야 합니다.")
        self.generate_video = generate_video
        self.download_video = download_video or self._download_video
        self.read_metadata = read_metadata
        try:
            self.square_output_strategy = SquareOutputStrategy(square_output_strategy)
        except ValueError as error:
            raise ValueError(
                "square_output_strategy는 reject 또는 center_crop이어야 합니다."
            ) from error
        self.normalize_video = normalize_video or (
            center_crop_square_video_to_vertical
            if production_mode
            else pad_video_to_vertical_canvas
        )
        self.publish_video = publish_video
        self.max_retries = max_retries
        self.production_mode = production_mode

    def run(self, request: VideoGenerationRequest) -> PipelineResult:
        expected_duration = self._script_duration(request.script)
        policy = (
            ValidationPolicy.production(expected_duration)
            if self.production_mode
            else ValidationPolicy(expected_duration_seconds=expected_duration)
        )
        last_validation: ValidationResult | None = None
        last_url = ""
        last_job_id = ""
        total_cost = 0.0
        last_provider_validation: ValidationResult | None = None
        last_source_metadata: VideoMetadata | None = None
        last_normalized_metadata: VideoMetadata | None = None
        last_source_normalized = False
        last_normalization_strategy: str | None = None

        for attempt in range(1, self.max_retries + 2):
            try:
                generated = self.generate_video(request, attempt)
            except VideoGenerationTimeoutError:
                # Do not submit another paid provider job while this one may
                # still be pending; the caller keeps the original job visible.
                raise
            last_url = generated.video_url
            last_job_id = generated.job_id
            total_cost += generated.cost or 0.0

            with tempfile.TemporaryDirectory(prefix="quedot-video-") as directory:
                try:
                    video_path = str(Path(directory) / "generated.mp4")
                    self.download_video(generated.video_url, video_path)
                    metadata = self.read_metadata(video_path)
                except Exception as error:
                    raise VideoValidationPipelineError(
                        "생성된 영상을 다운로드하거나 메타데이터를 읽지 못했습니다: "
                        f"{error}"
                    ) from error

                source_metadata = metadata
                provider_validation = validate_video(source_metadata, policy)
                provider_path = video_path
                normalized_path, normalization_strategy = self._normalize_if_needed(
                    video_path,
                    metadata,
                    policy,
                    directory,
                )
                source_normalized = normalized_path != provider_path
                if source_normalized:
                    video_path = normalized_path
                    metadata = self.read_metadata(video_path)

                last_validation = validate_video(metadata, policy)
                last_provider_validation = provider_validation
                last_source_metadata = source_metadata
                last_normalized_metadata = metadata if source_normalized else None
                last_source_normalized = source_normalized
                last_normalization_strategy = normalization_strategy
                logger.info(
                    "video validation result: provider_job_id=%s attempt=%s "
                    "source_metadata=%s validated_metadata=%s "
                    "provider_checks=%s provider_errors=%s checks=%s errors=%s",
                    generated.job_id,
                    attempt,
                    {
                        "width": getattr(source_metadata, "width", None),
                        "height": getattr(source_metadata, "height", None),
                        "duration_seconds": getattr(source_metadata, "duration_seconds", None),
                        "fps": getattr(source_metadata, "fps", None),
                        "codec": getattr(source_metadata, "codec", None),
                        "bitrate": getattr(source_metadata, "bitrate", None),
                        "black_frame_ratio": getattr(source_metadata, "black_frame_ratio", None),
                    },
                    {
                        "width": getattr(metadata, "width", None),
                        "height": getattr(metadata, "height", None),
                        "duration_seconds": getattr(metadata, "duration_seconds", None),
                        "fps": getattr(metadata, "fps", None),
                        "codec": getattr(metadata, "codec", None),
                        "bitrate": getattr(metadata, "bitrate", None),
                        "black_frame_ratio": getattr(metadata, "black_frame_ratio", None),
                    },
                    provider_validation.checks,
                    provider_validation.errors,
                    last_validation.checks,
                    last_validation.errors,
                )
                if last_validation.is_valid:
                    published: PublishedVideoArtifact | None = None
                    if self.publish_video is not None:
                        try:
                            published = self.publish_video(video_path, generated)
                        except Exception as error:
                            raise VideoValidationPipelineError(
                                f"검증된 영상을 저장하지 못했습니다: {error}"
                            ) from error

                    return PipelineResult(
                        status=PipelineStatus.COMPLETED,
                        attempts=attempt,
                        job_id=last_job_id,
                        video_url=(published.playback_url if published else last_url),
                        validation=last_validation,
                        total_cost=total_cost,
                        storage_path=(published.storage_path if published else None),
                        download_url=(published.download_url if published else None),
                        provider_validation=provider_validation,
                        source_normalized=source_normalized,
                        normalization_strategy=normalization_strategy,
                        source_metadata=source_metadata,
                        normalized_metadata=(metadata if source_normalized else None),
                    )

                # A wrong output shape cannot be fixed by repeating the same request.
                # Stop before another paid generation attempt is submitted.
                if "aspect_ratio" in last_validation.errors:
                    return PipelineResult(
                        status=PipelineStatus.RETRY_EXHAUSTED,
                        attempts=attempt,
                        job_id=last_job_id,
                        video_url=last_url,
                        validation=last_validation,
                        total_cost=total_cost,
                        provider_validation=provider_validation,
                        source_normalized=source_normalized,
                        normalization_strategy=normalization_strategy,
                        source_metadata=source_metadata,
                        normalized_metadata=(metadata if source_normalized else None),
                    )

                # A local normalization must never trigger another paid
                # provider request. Surface any remaining technical failure to
                # the caller with both source and normalized evidence intact.
                if source_normalized:
                    return PipelineResult(
                        status=PipelineStatus.RETRY_EXHAUSTED,
                        attempts=attempt,
                        job_id=last_job_id,
                        video_url=last_url,
                        validation=last_validation,
                        total_cost=total_cost,
                        provider_validation=provider_validation,
                        source_normalized=True,
                        normalization_strategy=normalization_strategy,
                        source_metadata=source_metadata,
                        normalized_metadata=metadata,
                    )

        assert last_validation is not None
        return PipelineResult(
            status=PipelineStatus.RETRY_EXHAUSTED,
            attempts=self.max_retries + 1,
            job_id=last_job_id,
            video_url=last_url,
            validation=last_validation,
            total_cost=total_cost,
            provider_validation=last_provider_validation,
            source_normalized=last_source_normalized,
            normalization_strategy=last_normalization_strategy,
            source_metadata=last_source_metadata,
            normalized_metadata=last_normalized_metadata,
        )

    def _normalize_if_needed(
        self,
        video_path: str,
        metadata: Any,
        policy: ValidationPolicy,
        directory: str,
    ) -> tuple[str, str | None]:
        if self.production_mode:
            expects_vertical = (
                policy.expected_aspect_width * 16
                == policy.expected_aspect_height * 9
            )
            should_center_crop = (
                self.square_output_strategy == SquareOutputStrategy.CENTER_CROP
                and expects_vertical
                and is_production_square_source(metadata)
            )
            if not should_center_crop:
                return video_path, None

            normalized_path = str(Path(directory) / "normalized.mp4")
            logger.info(
                "normalizing audited square video: strategy=%s source=%s target=1080x1920",
                SquareOutputStrategy.CENTER_CROP.value,
                {"width": metadata.width, "height": metadata.height},
            )
            try:
                self.normalize_video(video_path, normalized_path, metadata)
            except Exception as error:
                raise VideoValidationPipelineError(
                    f"정사각형 영상 center_crop 정규화에 실패했습니다: {error}"
                ) from error
            return normalized_path, SquareOutputStrategy.CENTER_CROP.value

        expected_aspect = policy.expected_aspect_width / policy.expected_aspect_height
        actual_aspect = metadata.width / metadata.height
        target_height = round(metadata.width / expected_aspect)
        if target_height % 2:
            target_height += 1

        should_pad = (
            metadata.height < target_height
            and 0 < actual_aspect - expected_aspect <= VERTICAL_PADDING_ASPECT_TOLERANCE
        )
        if not should_pad:
            return video_path, None

        normalized_path = str(Path(directory) / "normalized.mp4")
        logger.info(
            "normalizing short vertical video: source=%s target=%sx%s",
            {"width": metadata.width, "height": metadata.height},
            metadata.width,
            target_height,
        )
        try:
            self.normalize_video(video_path, normalized_path, metadata)
        except Exception as error:
            raise VideoValidationPipelineError(
                f"세로형 영상 여백 보정에 실패했습니다: {error}"
            ) from error
        return normalized_path, "vertical_padding"

    @staticmethod
    def _script_duration(script: Mapping[str, Any]) -> float:
        try:
            scenes = script["scenes"]
            return float(scenes[-1]["time_range_sec"]["end"])
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise VideoValidationPipelineError(
                "스크립트에서 기대 영상 길이를 읽지 못했습니다."
            ) from error

    @staticmethod
    def _download_video(url: str, destination: str) -> None:
        api_key = (
            os.getenv("OPENROUTER_VIDEO_API_KEY")
            or os.getenv("OPENROUTER_API_KEY")
            or app_settings.OPENROUTER_API_KEY
            or ""
        )
        request = Request(
            url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        with urlopen(request, timeout=120) as response, open(destination, "wb") as output:
            output.write(response.read())
