import unittest
from unittest.mock import patch

from app.api.v1.video import VideoGenerationBody, generate_video
from app.video_validation_pipeline import PipelineResult, PipelineStatus


class VideoApiTests(unittest.TestCase):
    @patch("app.api.v1.video.VideoValidationPipeline")
    @patch("app.api.v1.video.OpenRouterVideoClient.from_env")
    def test_generates_video_from_script_and_image(self, from_env, pipeline_class):
        pipeline_class.return_value.run.return_value = PipelineResult(
            status=PipelineStatus.COMPLETED,
            attempts=1,
            job_id="job-1",
            video_url="https://example.com/video.mp4",
            validation=type("Validation", (), {"checks": {}})(),
            total_cost=0.24,
        )
        body = VideoGenerationBody(
            script={
                "meta": {"aspect_ratio": "9:16"},
                "scenes": [{"time_range_sec": [0, 8], "visual": "상품", "subtitle": "소개"}],
            },
            image_url="https://example.com/product.jpg",
        )

        result = generate_video(body)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["video_url"], "https://example.com/video.mp4")
        self.assertEqual(result["cost"], 0.24)
        self.assertEqual(result["attempts"], 1)
        pipeline_request = pipeline_class.return_value.run.call_args.args[0]
        self.assertEqual(pipeline_request.image_url, "https://example.com/product.jpg")


if __name__ == "__main__":
    unittest.main()
