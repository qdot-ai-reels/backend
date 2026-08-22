import unittest
from unittest.mock import patch

from app.core.s3 import build_output_object_key, generate_presigned_url
from app.api.v1.video import (
    VideoGenerationBody,
    generate_video,
    get_video_access_url,
    publish_validated_video,
)
from app.settings_service import VideoModelCapabilities
from app.video_validation_pipeline import PipelineResult, PipelineStatus


class VideoApiTests(unittest.TestCase):
    def test_builds_outputs_key_from_provider_job_id(self):
        self.assertEqual(
            build_output_object_key("job-1"),
            "outputs/job-1/final.mp4",
        )
        unsafe_key = build_output_object_key("provider/job 1")
        self.assertTrue(unsafe_key.startswith("outputs/provider_job_1-"))
        self.assertTrue(unsafe_key.endswith("/final.mp4"))

    @patch("app.core.s3.get_s3_client")
    def test_presigned_download_url_sets_attachment_disposition(self, get_client):
        s3_client = get_client.return_value
        s3_client.generate_presigned_url.return_value = "https://s3.example.com/download"

        result = generate_presigned_url(
            "outputs/job-1/final.mp4",
            expiration=900,
            download=True,
        )

        self.assertEqual(result, "https://s3.example.com/download")
        params = s3_client.generate_presigned_url.call_args.kwargs["Params"]
        self.assertEqual(params["Key"], "outputs/job-1/final.mp4")
        self.assertEqual(
            params["ResponseContentDisposition"],
            'attachment; filename="final.mp4"',
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
            supported_resolutions=("720p",),
            generate_audio=False,
        )
        pipeline_class.return_value.run.return_value = PipelineResult(
            status=PipelineStatus.COMPLETED,
            attempts=1,
            job_id="job-1",
            video_url="https://s3.example.com/play",
            validation=type("Validation", (), {"checks": {}})(),
            total_cost=0.24,
            s3_object_key="outputs/job-1/final.mp4",
            download_url="https://s3.example.com/download",
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
        )

        result = generate_video(body)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["video_url"], "https://s3.example.com/play")
        self.assertEqual(result["download_url"], "https://s3.example.com/download")
        self.assertEqual(result["s3_object_key"], "outputs/job-1/final.mp4")
        self.assertEqual(result["cost"], 0.24)
        self.assertEqual(result["attempts"], 1)
        pipeline_request = pipeline_class.return_value.run.call_args.args[0]
        self.assertEqual(pipeline_request.image_url, "https://example.com/product.jpg")
        self.assertIs(
            pipeline_class.call_args.kwargs["publish_video"],
            publish_validated_video,
        )

    @patch("app.api.v1.video.generate_presigned_url")
    @patch("app.api.v1.video.upload_file_to_s3")
    def test_uploads_validated_video_to_outputs_and_returns_both_urls(
        self, upload_file, generate_url
    ):
        generate_url.side_effect = ["https://s3.example.com/play", "https://s3.example.com/download"]
        generated = type("Generated", (), {"job_id": "job-1"})()

        result = publish_validated_video("C:/tmp/generated.mp4", generated)

        upload_file.assert_called_once_with(
            "C:/tmp/generated.mp4",
            "outputs/job-1/final.mp4",
            content_type="video/mp4",
        )
        self.assertEqual(result.object_key, "outputs/job-1/final.mp4")
        self.assertEqual(result.playback_url, "https://s3.example.com/play")
        self.assertEqual(result.download_url, "https://s3.example.com/download")
        self.assertTrue(generate_url.call_args_list[1].kwargs["download"])

    @patch("app.api.v1.video.generate_presigned_url", return_value="https://s3.example.com/new")
    def test_reissues_download_url_for_completed_video(self, generate_url):
        result = get_video_access_url("job-1", download=True)

        self.assertEqual(result["s3_object_key"], "outputs/job-1/final.mp4")
        self.assertEqual(result["url"], "https://s3.example.com/new")
        self.assertTrue(result["download"])
        self.assertTrue(generate_url.call_args.kwargs["download"])


if __name__ == "__main__":
    unittest.main()
