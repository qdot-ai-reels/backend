import json
import unittest

from app.video_generator import (
    VideoGenerationRequest,
    VideoGenerationResult,
    VideoGenerationError,
    OpenRouterVideoClient,
    build_video_prompt,
)


SCRIPT = {
    "meta": {"aspect_ratio": "9:16", "max_duration_sec": 30},
    "summary": "상품을 소개하는 영상",
    "scenes": [
        {
            "scene_number": 1,
            "time_range_sec": [0, 8],
            "visual": "상품을 화면 중앙에 보여준다.",
            "subtitle": "상품 소개",
            "voiceover": "상품을 소개합니다.",
            "intent": "hook",
        }
    ],
}


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class SequentialOpener:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        return FakeResponse(next(self.payloads))


class VideoGeneratorTests(unittest.TestCase):
    def test_builds_prompt_from_script_scenes(self):
        prompt = build_video_prompt(SCRIPT)

        self.assertIn("상품을 화면 중앙에 보여준다.", prompt)
        self.assertIn("상품 소개", prompt)
        self.assertIn("9:16", prompt)

    def test_submits_video_job_and_polls_until_completed(self):
        opener = SequentialOpener([
            {"id": "job-1", "polling_url": "https://example.com/poll/job-1", "status": "pending"},
            {"id": "job-1", "status": "pending"},
            {
                "id": "job-1",
                "status": "completed",
                "unsigned_urls": ["https://example.com/video.mp4"],
                "usage": {"cost": 0.24},
            },
        ])
        client = OpenRouterVideoClient(
            api_key="test-key",
            model="google/veo-3.1-lite",
            opener=opener,
            sleeper=lambda _seconds: None,
        )

        result = client.generate_video(VideoGenerationRequest(script=SCRIPT, image_url="https://example.com/product.jpg"))

        self.assertIsInstance(result, VideoGenerationResult)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.video_url, "https://example.com/video.mp4")
        self.assertEqual(len(opener.requests), 3)
        request_body = json.loads(opener.requests[0].data)
        self.assertEqual(request_body["duration"], 8)
        self.assertEqual(request_body["aspect_ratio"], "9:16")
        self.assertFalse(request_body["generate_audio"])
        self.assertEqual(request_body["frame_images"][0]["frame_type"], "first_frame")

    def test_raises_when_video_job_fails(self):
        opener = SequentialOpener([
            {"id": "job-1", "polling_url": "https://example.com/poll/job-1", "status": "pending"},
            {"id": "job-1", "status": "failed", "error": {"message": "provider failed"}},
        ])
        client = OpenRouterVideoClient(
            api_key="test-key",
            model="google/veo-3.1-lite",
            opener=opener,
            sleeper=lambda _seconds: None,
        )

        with self.assertRaises(VideoGenerationError):
            client.generate_video(VideoGenerationRequest(script=SCRIPT, image_url="https://example.com/product.jpg"))

    def test_uses_last_scene_end_as_video_duration(self):
        opener = SequentialOpener([
            {"id": "job-1", "polling_url": "https://example.com/poll/job-1", "status": "pending"},
            {"id": "job-1", "status": "completed", "unsigned_urls": ["https://example.com/video.mp4"]},
        ])
        client = OpenRouterVideoClient(api_key="test-key", opener=opener, sleeper=lambda _seconds: None)

        client.generate_video(VideoGenerationRequest(script=SCRIPT, image_url="https://example.com/product.jpg"))

        request_body = json.loads(opener.requests[0].data)
        self.assertEqual(request_body["duration"], 8)

    def test_rejects_invalid_script_before_submitting_video_job(self):
        opener = SequentialOpener([])
        client = OpenRouterVideoClient(
            api_key="test-key",
            opener=opener,
            sleeper=lambda _seconds: None,
        )

        with self.assertRaises(VideoGenerationError):
            client.generate_video(
                VideoGenerationRequest(
                    script={"meta": {"aspect_ratio": "9:16"}, "scenes": []},
                    image_url="https://example.com/product.jpg",
                )
            )

        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
