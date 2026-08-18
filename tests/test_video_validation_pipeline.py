import json
import tempfile
import unittest
from pathlib import Path

from app.video_generator import VideoGenerationRequest, VideoGenerationResult
from app.video_validation_pipeline import (
    PipelineStatus,
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

    def test_regenerates_when_downloaded_video_fails_validation(self):
        metadata = iter([VideoMetadata(1280, 720, 8.0), VideoMetadata(720, 1280, 8.0)])
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

    def test_does_not_retry_after_validation_failures_are_exhausted(self):
        pipeline = VideoValidationPipeline(
            generate_video=lambda _request, attempt: VideoGenerationResult(
                job_id=f"job-{attempt}", status="completed",
                video_url="https://example.com/video.mp4", cost=0.24,
            ),
            download_video=lambda _url, destination: Path(destination).write_bytes(b"video"),
            read_metadata=lambda _path: VideoMetadata(720, 1280, 7.0),
            max_retries=1,
        )

        result = pipeline.run(VideoGenerationRequest(SCRIPT, "https://example.com/product.jpg"))

        self.assertEqual(result.status, PipelineStatus.RETRY_EXHAUSTED)
        self.assertEqual(result.attempts, 2)
        self.assertIn("duration", result.validation.errors)

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
