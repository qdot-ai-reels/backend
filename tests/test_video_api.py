import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.api.v1.video import (
    LOCAL_VIDEO_OUTPUT_DIR,
    VideoGenerationBody,
    generate_video,
    get_video_file,
    get_video_access_url,
    publish_validated_video,
)
from app.settings_service import VideoModelCapabilities
from app.video_validation_pipeline import PipelineResult, PipelineStatus


class VideoApiTests(unittest.TestCase):
    @patch("app.api.v1.video.VideoValidationPipeline")
    @patch("app.api.v1.video.get_video_model_capabilities")
    @patch("app.api.v1.video.build_video_client")
    def test_generates_video_from_script_and_image(self, build_client, get_capabilities, pipeline_class):
        get_capabilities.return_value = VideoModelCapabilities(
            model_id="video-model",
            name="Video Model",
            supported_durations=(4, 6, 8),
            supported_aspect_ratios=("9:16",),
            supported_resolutions=("720p",),
            generate_audio=False,
        )
        pipeline_class.return_value.run.return_value = PipelineResult(
            status=PipelineStatus.COMPLETED,
            attempts=1,
            job_id="job-1",
            video_url="/api/v1/reels/video/job-1/file",
            validation=type("Validation", (), {"checks": {}})(),
            total_cost=0.24,
            storage_path="runtime/videos/job-1/final.mp4",
            download_url="/api/v1/reels/video/job-1/file?download=true",
        )
        body = VideoGenerationBody(
            script={
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
                    "visual": "상품",
                    "auditory": {"subtitle": "소개", "voiceover": "소개합니다."},
                    "notes": "소개",
                }],
                "compliance_notes": {"avoid": [], "focus": []},
            },
            image_url="https://example.com/product.jpg",
            influencer_image_url="https://example.com/influencer.jpg",
        )

        result = generate_video(body)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["video_url"], "/api/v1/reels/video/job-1/file")
        self.assertEqual(result["download_url"], "/api/v1/reels/video/job-1/file?download=true")
        self.assertEqual(result["storage_path"], "runtime/videos/job-1/final.mp4")
        self.assertEqual(result["cost"], 0.24)
        self.assertEqual(result["attempts"], 1)
        pipeline_request = pipeline_class.return_value.run.call_args.args[0]
        self.assertEqual(pipeline_request.image_url, "https://example.com/product.jpg")
        self.assertEqual(
            pipeline_request.influencer_image_url,
            "https://example.com/influencer.jpg",
        )
        self.assertIs(
            pipeline_class.call_args.kwargs["publish_video"],
            publish_validated_video,
        )

    def test_saves_validated_video_locally(self):
        generated = type("Generated", (), {"job_id": "job-1"})()
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.api.v1.video.LOCAL_VIDEO_OUTPUT_DIR", Path(directory)
        ):
            source = Path(directory) / "generated.mp4"
            source.write_bytes(b"video")
            result = publish_validated_video(str(source), generated)

        self.assertEqual(result.storage_path.endswith("job-1/final.mp4"), True)
        self.assertEqual(result.playback_url, "/api/v1/reels/video/job-1/file")
        self.assertEqual(result.download_url, "/api/v1/reels/video/job-1/file?download=true")

    def test_returns_local_video_url(self):
        result = get_video_access_url("job-1", download=True)

        self.assertEqual(result["storage"], "local")
        self.assertEqual(result["url"], "/api/v1/reels/video/job-1/file?download=true")
        self.assertTrue(result["download"])


if __name__ == "__main__":
    unittest.main()
