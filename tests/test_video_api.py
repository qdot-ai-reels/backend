import unittest
from unittest.mock import patch

from app.api.v1.video import VideoGenerationBody, generate_video
from app.video_generator import VideoGenerationResult


class VideoApiTests(unittest.TestCase):
    @patch("app.api.v1.video.OpenRouterVideoClient.from_env")
    def test_generates_video_from_script_and_image(self, from_env):
        client = from_env.return_value
        client.generate_video.return_value = VideoGenerationResult(
            job_id="job-1",
            status="completed",
            video_url="https://example.com/video.mp4",
            cost=0.24,
        )
        body = VideoGenerationBody(
            script={"meta": {"aspect_ratio": "9:16"}, "scenes": []},
            image_url="https://example.com/product.jpg",
        )

        result = generate_video(body)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["video_url"], "https://example.com/video.mp4")
        self.assertEqual(result["cost"], 0.24)
        request = client.generate_video.call_args.args[0]
        self.assertEqual(request.image_url, "https://example.com/product.jpg")
        self.assertEqual(request.duration_seconds, 8)


if __name__ == "__main__":
    unittest.main()
