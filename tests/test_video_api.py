import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.api.v1.video import (
    LOCAL_VIDEO_OUTPUT_DIR,
    VideoGenerationBody,
    generate_video,
    get_video_file,
    get_video_access_url,
    publish_validated_video,
    select_video_resolution,
)
from app.settings_service import VideoModelCapabilities
from app.video_validation_pipeline import PipelineResult, PipelineStatus


class VideoApiTests(unittest.TestCase):
    def test_uses_480p_when_runtime_settings_are_unavailable(self):
        capabilities = VideoModelCapabilities(
            model_id="video-model",
            name="Video Model",
            supported_durations=(4, 6, 8, 15),
            supported_aspect_ratios=("9:16",),
            supported_resolutions=("480p", "720p", "1080p"),
            generate_audio=False,
        )

        self.assertEqual(select_video_resolution(None, capabilities), "480p")

    def test_only_accepts_vertical_nine_by_sixteen_requests(self):
        with self.assertRaises(ValidationError):
            VideoGenerationBody(
                script={"scenes": [{"time_range_sec": {"start": 0, "end": 8}}]},
                image_url="https://example.com/product.jpg",
                influencer_image_url="https://example.com/influencer.jpg",
                aspect_ratio="1:1",
            )

    @patch("app.api.v1.video.VideoValidationPipeline")
    @patch("app.api.v1.video.get_video_model_capabilities")
    @patch("app.api.v1.video.build_video_client")
    def test_generates_video_from_script_and_image(self, build_client, get_capabilities, pipeline_class):
        get_capabilities.return_value = VideoModelCapabilities(
            model_id="video-model",
            name="Video Model",
            supported_durations=(4, 6, 8),
            supported_aspect_ratios=("9:16",),
            supported_resolutions=("480p", "720p"),
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
            import subprocess
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(source),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            result = publish_validated_video(str(source), generated)

        self.assertEqual(result.storage_path.endswith("job-1/final.mp4"), True)
        self.assertEqual(result.playback_url, "/api/v1/reels/video/job-1/file")
        self.assertEqual(result.download_url, "/api/v1/reels/video/job-1/file?download=true")
        self.assertEqual(self._stream_count(Path(result.storage_path), "a:0"), 0)

    @staticmethod
    def _stream_count(path, selector):
        import subprocess
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", selector,
                "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return len([line for line in result.stdout.splitlines() if line.strip()])

    def test_returns_local_video_url(self):
        result = get_video_access_url("job-1", download=True)

        self.assertEqual(result["storage"], "local")
        self.assertEqual(result["url"], "/api/v1/reels/video/job-1/file?download=true")
        self.assertTrue(result["download"])


if __name__ == "__main__":
    unittest.main()
