"""Connect video generation, MP4 metadata validation, and regeneration."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen

from app.video_generator import VideoGenerationRequest, VideoGenerationResult
from app.video_metadata import read_video_metadata
from app.video_validator import ValidationPolicy, ValidationResult, validate_video


class VideoValidationPipelineError(RuntimeError):
    """Raised when a generated video cannot be downloaded or inspected."""


class PipelineStatus(str, Enum):
    COMPLETED = "completed"
    RETRY_EXHAUSTED = "retry_exhausted"


@dataclass(frozen=True)
class PipelineResult:
    status: PipelineStatus
    attempts: int
    job_id: str
    video_url: str
    validation: ValidationResult
    total_cost: float


VideoGenerator = Callable[[VideoGenerationRequest, int], VideoGenerationResult]
VideoDownloader = Callable[[str, str], None]
MetadataReader = Callable[[str | Path], Any]


class VideoValidationPipeline:
    def __init__(
        self,
        generate_video: VideoGenerator,
        download_video: VideoDownloader | None = None,
        read_metadata: MetadataReader = read_video_metadata,
        max_retries: int = 1,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries는 0 이상이어야 합니다.")
        self.generate_video = generate_video
        self.download_video = download_video or self._download_video
        self.read_metadata = read_metadata
        self.max_retries = max_retries

    def run(self, request: VideoGenerationRequest) -> PipelineResult:
        expected_duration = self._script_duration(request.script)
        policy = ValidationPolicy(expected_duration_seconds=expected_duration)
        last_validation: ValidationResult | None = None
        last_url = ""
        last_job_id = ""
        total_cost = 0.0

        for attempt in range(1, self.max_retries + 2):
            generated = self.generate_video(request, attempt)
            last_url = generated.video_url
            last_job_id = generated.job_id
            total_cost += generated.cost or 0.0

            try:
                with tempfile.TemporaryDirectory(prefix="quedot-video-") as directory:
                    video_path = str(Path(directory) / "generated.mp4")
                    self.download_video(generated.video_url, video_path)
                    metadata = self.read_metadata(video_path)
            except Exception as error:
                raise VideoValidationPipelineError(
                    f"생성된 영상을 다운로드하거나 메타데이터를 읽지 못했습니다: {error}"
                ) from error

            last_validation = validate_video(metadata, policy)
            if last_validation.is_valid:
                return PipelineResult(
                    status=PipelineStatus.COMPLETED,
                    attempts=attempt,
                    job_id=last_job_id,
                    video_url=last_url,
                    validation=last_validation,
                    total_cost=total_cost,
                )

        assert last_validation is not None
        return PipelineResult(
            status=PipelineStatus.RETRY_EXHAUSTED,
            attempts=self.max_retries + 1,
            job_id=last_job_id,
            video_url=last_url,
            validation=last_validation,
            total_cost=total_cost,
        )

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
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        request = Request(
            url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        with urlopen(request, timeout=120) as response, open(destination, "wb") as output:
            output.write(response.read())
