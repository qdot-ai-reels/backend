import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.video_generator import (
    VideoGenerationRequest,
    VideoGenerationResult,
    VideoGenerationTimeoutError,
)
from app.video_validation_pipeline import (
    PipelineStatus,
    PublishedVideoArtifact,
    SquareOutputStrategy,
    VideoValidationPipeline,
    VideoValidationPipelineError,
)
from app.video_validator import VideoMetadata


SCRIPT = {
    "meta": {
        "output_format_version": "1.0",
        "framework": "Hook-Body-CTA",
        "language": "ko",
    },
    "summary": {
        "main_target": "보호자",
        "pain_point": "고민",
        "product_usp": "장점",
        "key_message": "메시지",
        "tone_and_manner": "분위기",
    },
    "scenes": [{
        "scene_name": "Hook",
        "time_range_sec": {"start": 0, "end": 8},
        "visual": "상품을 보여준다.",
        "auditory": {"subtitle": "상품 소개", "voiceover": "상품을 소개합니다."},
        "notes": "상품 소개",
    }],
    "compliance_notes": {"avoid": [], "focus": []},
}


class VideoValidationPipelineTests(unittest.TestCase):
    def test_center_crops_audited_square_source_without_another_paid_attempt(self):
        attempts = []
        normalized = []
        published = []

        def read_metadata(path):
            if str(path).endswith("normalized.mp4"):
                return VideoMetadata(
                    1080, 1920, 8.0, fps=30.0, codec="h264",
                    bitrate=8_000_000, black_frame_ratio=0.0,
                )
            return VideoMetadata(
                1440, 1440, 8.0, fps=30.0, codec="h264",
                bitrate=8_000_000, black_frame_ratio=0.0,
            )

        def normalize_video(source, destination, metadata):
            normalized.append((source, destination, metadata))
            Path(destination).write_bytes(b"normalized")

        def publish_video(path, _generated):
            published.append(Path(path).name)
            return PublishedVideoArtifact(
                storage_path=str(path),
                playback_url="/video/file",
                download_url="/video/file?download=true",
            )

        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, attempt: (
                attempts.append(attempt)
                or VideoGenerationResult(
                    job_id="job-1",
                    status="completed",
                    video_url="https://example.com/square.mp4",
                    cost=1.0,
                )
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=read_metadata,
            normalize_video=normalize_video,
            publish_video=publish_video,
            max_retries=2,
            production_mode=True,
            square_output_strategy=SquareOutputStrategy.CENTER_CROP,
        )

        result = pipeline.run(
            VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg")
        )

        self.assertEqual(result.status, PipelineStatus.COMPLETED)
        self.assertEqual(attempts, [1])
        self.assertEqual(len(normalized), 1)
        self.assertEqual(published, ["normalized.mp4"])
        self.assertTrue(result.source_normalized)
        self.assertEqual(result.normalization_strategy, "center_crop")
        self.assertEqual((result.source_metadata.width, result.source_metadata.height), (1440, 1440))
        self.assertEqual((result.normalized_metadata.width, result.normalized_metadata.height), (1080, 1920))
        self.assertIn("aspect_ratio", result.provider_validation.errors)
        self.assertIn("resolution", result.provider_validation.errors)
        self.assertEqual(result.validation.errors, [])

    def test_center_crop_strategy_refuses_low_resolution_and_wide_sources(self):
        for width, height in ((1079, 1079), (1920, 1080)):
            with self.subTest(width=width, height=height):
                attempts = []
                normalized = []
                pipeline = VideoValidationPipeline(
                    generate_video=lambda _request, attempt: (
                        attempts.append(attempt)
                        or VideoGenerationResult(
                            job_id="job-1",
                            status="completed",
                            video_url="https://example.com/video.mp4",
                            cost=1.0,
                        )
                    ),
                    download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
                    read_metadata=lambda _path: VideoMetadata(
                        width, height, 8.0, fps=30.0, codec="h264",
                        bitrate=8_000_000, black_frame_ratio=0.0,
                    ),
                    normalize_video=lambda *_args: normalized.append(True),
                    max_retries=2,
                    production_mode=True,
                    square_output_strategy="center_crop",
                )

                result = pipeline.run(
                    VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg")
                )

                self.assertEqual(result.status, PipelineStatus.RETRY_EXHAUSTED)
                self.assertEqual(attempts, [1])
                self.assertEqual(normalized, [])
                self.assertFalse(result.source_normalized)
                self.assertIn("aspect_ratio", result.validation.errors)

    def test_production_square_output_is_rejected_without_explicit_opt_in(self):
        normalized = []
        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, _attempt: VideoGenerationResult(
                job_id="job-1",
                status="completed",
                video_url="https://example.com/square.mp4",
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(
                1440, 1440, 8.0, fps=30.0, codec="h264",
                bitrate=8_000_000, black_frame_ratio=0.0,
            ),
            normalize_video=lambda *_args: normalized.append(True),
            max_retries=0,
            production_mode=True,
        )

        result = pipeline.run(
            VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg")
        )

        self.assertEqual(result.status, PipelineStatus.RETRY_EXHAUSTED)
        self.assertEqual(normalized, [])
        self.assertFalse(result.source_normalized)

    def test_center_crop_strategy_leaves_valid_vertical_source_untouched(self):
        normalized = []
        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, _attempt: VideoGenerationResult(
                job_id="job-1",
                status="completed",
                video_url="https://example.com/vertical.mp4",
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(
                1080, 1920, 8.0, fps=30.0, codec="h264",
                bitrate=8_000_000, black_frame_ratio=0.0,
            ),
            normalize_video=lambda *_args: normalized.append(True),
            max_retries=0,
            production_mode=True,
            square_output_strategy="center_crop",
        )

        result = pipeline.run(
            VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg")
        )

        self.assertEqual(result.status, PipelineStatus.COMPLETED)
        self.assertEqual(normalized, [])
        self.assertFalse(result.source_normalized)

    def test_failed_normalized_artifact_never_submits_another_paid_attempt(self):
        attempts = []
        published = []

        def read_metadata(path):
            if str(path).endswith("normalized.mp4"):
                return VideoMetadata(
                    1080, 1920, 8.0, fps=30.0, codec="h264",
                    bitrate=1_000_000, black_frame_ratio=0.0,
                )
            return VideoMetadata(
                1440, 1440, 8.0, fps=30.0, codec="h264",
                bitrate=8_000_000, black_frame_ratio=0.0,
            )

        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, attempt: (
                attempts.append(attempt)
                or VideoGenerationResult(
                    job_id="job-1",
                    status="completed",
                    video_url="https://example.com/square.mp4",
                    cost=1.0,
                )
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=read_metadata,
            normalize_video=lambda _source, destination, _metadata: Path(destination).write_bytes(b"normalized"),
            publish_video=lambda *_args: published.append(True),
            max_retries=2,
            production_mode=True,
            square_output_strategy="center_crop",
        )

        result = pipeline.run(
            VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg")
        )

        self.assertEqual(result.status, PipelineStatus.RETRY_EXHAUSTED)
        self.assertEqual(attempts, [1])
        self.assertEqual(published, [])
        self.assertTrue(result.source_normalized)
        self.assertIn("bitrate", result.validation.errors)

    def test_logs_metadata_and_validation_errors(self):
        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, _attempt: VideoGenerationResult(
                job_id="job-1",
                status="completed",
                video_url="https://example.com/video.mp4",
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(480, 854, 7.0),
            max_retries=0,
        )

        with patch("app.video_validation_pipeline.logger") as logger:
            result = pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertEqual(result.status, PipelineStatus.RETRY_EXHAUSTED)
        logger.info.assert_called_once()
        log_message = logger.info.call_args.args
        self.assertIn("video validation result", log_message[0])
        self.assertEqual(log_message[3], {
            "width": 480,
            "height": 854,
            "duration_seconds": 7.0,
            "fps": None,
            "codec": None,
            "bitrate": None,
            "black_frame_ratio": None,
        })
        self.assertIn("duration", log_message[6])

    def test_pads_short_vertical_video_before_publishing(self):
        normalized = []

        def read_metadata(path):
            if str(path).endswith("normalized.mp4"):
                return VideoMetadata(480, 854, 8.0)
            return VideoMetadata(480, 832, 8.0)

        def normalize_video(source, destination, metadata):
            normalized.append((source, destination, metadata))
            Path(destination).write_bytes(b"normalized video")

        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, _attempt: VideoGenerationResult(
                job_id="job-1",
                status="completed",
                video_url="https://example.com/video.mp4",
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=read_metadata,
            normalize_video=normalize_video,
            publish_video=lambda path, _generated: PublishedVideoArtifact(
                storage_path=str(path),
                playback_url="/video/file",
                download_url="/video/file?download=true",
            ),
        )

        result = pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertEqual(result.status, PipelineStatus.COMPLETED)
        self.assertEqual(len(normalized), 1)
        self.assertTrue(result.storage_path.endswith("normalized.mp4"))

    def test_does_not_submit_duplicate_provider_jobs_after_timeout(self):
        attempts = []

        def generate_video(_request, attempt):
            attempts.append(attempt)
            raise VideoGenerationTimeoutError("polling timeout", job_id="job-1")

        pipeline = VideoValidationPipeline(
            generate_video=generate_video,
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(720, 1280, 8.0),
            max_retries=2,
        )

        with self.assertRaises(VideoGenerationTimeoutError):
            pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertEqual(attempts, [1])

    def test_returns_completed_when_downloaded_video_passes_validation(self):
        generated_urls = []

        def generate_video(_request, attempt):
            generated_urls.append(attempt)
            return VideoGenerationResult(
                job_id=f"job-{attempt}",
                status="completed",
                video_url=f"https://example.com/video-{attempt}.mp4",
                cost=0.24,
            )

        pipeline = VideoValidationPipeline(
            generate_video=generate_video,
            download_video=lambda url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(720, 1280, 8.0),
        )

        result = pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertEqual(result.status, PipelineStatus.COMPLETED)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.video_url, "https://example.com/video-1.mp4")
        self.assertEqual(result.validation.errors, [])
        self.assertEqual(generated_urls, [1])

    def test_publishes_only_the_validated_video_before_temp_cleanup(self):
        published_paths = []

        def publish_video(video_path, generated):
            path = Path(video_path)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"video")
            published_paths.append((path.name, generated.job_id))
            return PublishedVideoArtifact(
                storage_path="runtime/videos/job-1/final.mp4",
                playback_url="/api/v1/reels/video/job-1/file",
                download_url="/api/v1/reels/video/job-1/file?download=true",
            )

        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, _attempt: VideoGenerationResult(
                job_id="job-1",
                status="completed",
                video_url="https://provider.example.com/video.mp4",
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(720, 1280, 8.0),
            publish_video=publish_video,
        )

        result = pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertEqual(result.video_url, "/api/v1/reels/video/job-1/file")
        self.assertEqual(result.download_url, "/api/v1/reels/video/job-1/file?download=true")
        self.assertEqual(result.storage_path, "runtime/videos/job-1/final.mp4")
        self.assertEqual(published_paths, [("generated.mp4", "job-1")])

    def test_regenerates_when_downloaded_video_fails_validation(self):
        metadata = iter([VideoMetadata(720, 1280, 7.0), VideoMetadata(720, 1280, 8.0)])
        attempts = []

        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, attempt: (
                attempts.append(attempt) or VideoGenerationResult(
                    job_id=f"job-{attempt}", status="completed",
                    video_url=f"https://example.com/video-{attempt}.mp4", cost=0.24,
                )
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: next(metadata),
            max_retries=1,
        )

        result = pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertEqual(result.status, PipelineStatus.COMPLETED)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.video_url, "https://example.com/video-2.mp4")
        self.assertEqual(attempts, [1, 2])

    def test_stops_without_retry_when_aspect_ratio_is_invalid(self):
        attempts = []

        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, attempt: (
                attempts.append(attempt) or VideoGenerationResult(
                    job_id=f"job-{attempt}",
                    status="completed",
                    video_url=f"https://example.com/video-{attempt}.mp4",
                    cost=1.0,
                )
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(960, 960, 8.0),
            max_retries=1,
        )

        result = pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertEqual(result.status, PipelineStatus.RETRY_EXHAUSTED)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.total_cost, 1.0)
        self.assertEqual(attempts, [1])
        self.assertIn("aspect_ratio", result.validation.errors)

    def test_accepts_480p_vertical_dimensions_with_rounding(self):
        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, _attempt: VideoGenerationResult(
                job_id="job-1",
                status="completed",
                video_url="https://example.com/video.mp4",
                cost=0.24,
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(480, 854, 8.0),
            publish_video=lambda video_path, _generated: PublishedVideoArtifact(
                storage_path=video_path,
                playback_url="/video/file",
                download_url="/video/file?download=true",
            ),
        )

        result = pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertEqual(result.status, PipelineStatus.COMPLETED)
        self.assertNotIn("aspect_ratio", result.validation.errors)

    def test_fails_completed_flow_when_local_publish_fails(self):
        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, _attempt: VideoGenerationResult(
                job_id="job-1",
                status="completed",
                video_url="https://provider.example.com/video.mp4",
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(720, 1280, 8.0),
            publish_video=lambda *_args: (_ for _ in ()).throw(
                ValueError("mock local storage failure")
            ),
        )

        with self.assertRaises(VideoValidationPipelineError) as context:
            pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertIn("저장", str(context.exception))

    def test_does_not_retry_after_validation_failures_are_exhausted(self):
        published = []
        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, attempt: VideoGenerationResult(
                job_id=f"job-{attempt}", status="completed",
                video_url="https://example.com/video.mp4", cost=0.24,
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(720, 1280, 7.0),
            publish_video=lambda *_args: published.append(True),
            max_retries=1,
        )

        result = pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertEqual(result.status, PipelineStatus.RETRY_EXHAUSTED)
        self.assertEqual(result.attempts, 2)
        self.assertIn("duration", result.validation.errors)
        self.assertEqual(published, [])

    def test_raises_when_download_fails(self):
        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, _attempt: VideoGenerationResult(
                job_id="job-1", status="completed",
                video_url="https://example.com/video.mp4", cost=0.24,
            ),
            download_video=lambda _url, _destination: (_ for _ in ()).throw(
                OSError("download failed")
            ),
            read_metadata=lambda _path: VideoMetadata(720, 1280, 8.0),
        )

        with self.assertRaises(VideoValidationPipelineError):
            pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))


if __name__ == "__main__":
    unittest.main()
