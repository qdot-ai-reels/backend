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
        self.assertEqual(result["video_url"], "https://example.com/video.mp4")
        self.assertEqual(result["cost"], 0.24)
        self.assertEqual(result["attempts"], 1)
        pipeline_request = pipeline_class.return_value.run.call_args.args[0]
        self.assertEqual(pipeline_request.image_url, "https://example.com/product.jpg")


if __name__ == "__main__":
    unittest.main()
