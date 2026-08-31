import json
import unittest

from app.video_generator import (
    VideoGenerationRequest,
    VideoGenerationResult,
    VideoGenerationError,
    VideoGenerationTimeoutError,
    OpenRouterVideoClient,
    build_video_prompt,
)


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
    "scenes": [
        {
            "scene_name": "Hook",
            "time_range_sec": {"start": 0, "end": 8},
            "visual": "상품을 화면 중앙에 보여준다.",
            "auditory": {
                "subtitle": "상품 소개",
                "voiceover": "상품을 소개합니다.",
            },
            "notes": "상품 소개",
        }
    ],
    "compliance_notes": {"avoid": [], "focus": []},
}

CURRENT_SCRIPT = {
    "meta": {"output_format_version": "1.0", "language": "ko"},
    "product": {"usp": "스파우트 30포 구성"},
    "customer": {"main_target": "보호자", "pain_point": "상품 선택 고민"},
    "ads": {"goal": "상품 소개", "cta_action": "상품 확인"},
    "video": {
        "video_duration": "15초",
        "required_scenes_elements": "상품 라벨",
        "forbidden_scenes_elements": "허위 효능",
    },
    "etc": {"additional_information": None, "video_ads_methodology": "Hook-Body-CTA"},
    "scenes": SCRIPT["scenes"],
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
    def setUp(self):
        self.image_dimensions_reader = lambda _image_url: (720, 1280)

    def test_defaults_to_six_minute_video_poll_window(self):
        client = OpenRouterVideoClient(api_key="test-key")

        self.assertEqual(client.poll_interval_seconds, 5.0)
        self.assertEqual(client.max_poll_attempts, 72)

    def test_custom_poll_window_reports_timeout_metadata(self):
        class NeverCompletesOpener:
            def __init__(self):
                self.requests = []

            def __call__(self, request, timeout):
                self.requests.append(request)
                if len(self.requests) == 1:
                    return FakeResponse({
                        "id": "job-1",
                        "polling_url": "https://example.com/poll/job-1",
                    })
                return FakeResponse({"id": "job-1", "status": "pending"})

        opener = NeverCompletesOpener()
        client = OpenRouterVideoClient(
            api_key="test-key",
            max_poll_attempts=72,
            opener=opener,
            sleeper=lambda _seconds: None,
            image_dimensions_reader=self.image_dimensions_reader,
        )

        with self.assertRaisesRegex(
            VideoGenerationTimeoutError, "polling 시간이 초과"
        ) as context:
            client.generate_video(
                VideoGenerationRequest(
                    script=SCRIPT,
                    image_url="https://example.com/product.jpg",
                )
            )

        self.assertEqual(len(opener.requests), 73)
        self.assertEqual(context.exception.job_id, "job-1")
        self.assertEqual(context.exception.last_status, "pending")
        self.assertEqual(context.exception.poll_count, 72)

    def test_builds_prompt_from_script_scenes(self):
        prompt = build_video_prompt(SCRIPT)

        self.assertIn("상품을 화면 중앙에 보여준다.", prompt)
        self.assertNotIn("자막=상품 소개", prompt)
        self.assertNotIn("내레이션=상품을 소개합니다.", prompt)
        self.assertIn("Do not add subtitles, captions, prices, discounts, CTA text, or dialogue.", prompt)
        self.assertIn("9:16", prompt)

    def test_includes_full_script_context_in_video_prompt(self):
        prompt = build_video_prompt(CURRENT_SCRIPT)

        self.assertIn("스파우트 30포 구성", prompt)
        self.assertIn("상품 라벨", prompt)
        self.assertIn("Hook-Body-CTA", prompt)

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
            model="bytedance/seedance-2.0-mini",
            opener=opener,
            sleeper=lambda _seconds: None,
            image_dimensions_reader=self.image_dimensions_reader,
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
        self.assertEqual(
            request_body["input_references"][0]["image_url"]["url"],
            "https://example.com/product.jpg",
        )
        self.assertNotIn("frame_images", request_body)

    def test_uses_frame_image_for_veo_model(self):
        opener = SequentialOpener([
            {"id": "job-1", "polling_url": "https://example.com/poll/job-1"},
            {
                "id": "job-1",
                "status": "completed",
                "unsigned_urls": ["https://example.com/video.mp4"],
            },
        ])
        client = OpenRouterVideoClient(
            api_key="test-key",
            model="google/veo-3.1-lite",
            opener=opener,
            sleeper=lambda _seconds: None,
            image_dimensions_reader=self.image_dimensions_reader,
        )

        client.generate_video(
            VideoGenerationRequest(
                script=SCRIPT,
                image_url="https://example.com/product.jpg",
            )
        )

        request_body = json.loads(opener.requests[0].data)
        self.assertEqual(
            request_body["frame_images"][0]["image_url"]["url"],
            "https://example.com/product.jpg",
        )
        self.assertEqual(request_body["frame_images"][0]["frame_type"], "first_frame")
        self.assertNotIn("input_references", request_body)

    def test_submits_product_and_influencer_as_reference_images(self):
        opener = SequentialOpener([
            {"id": "job-1", "polling_url": "https://example.com/poll/job-1", "status": "pending"},
            {"id": "job-1", "status": "completed", "unsigned_urls": ["https://example.com/video.mp4"]},
        ])
        client = OpenRouterVideoClient(
            api_key="test-key",
            opener=opener,
            sleeper=lambda _seconds: None,
            image_dimensions_reader=self.image_dimensions_reader,
        )

        client.generate_video(
            VideoGenerationRequest(
                script=SCRIPT,
                image_url="https://example.com/product.jpg",
                influencer_image_url="https://example.com/influencer.jpg",
            )
        )

        request_body = json.loads(opener.requests[0].data)
        self.assertNotIn("frame_images", request_body)
        self.assertEqual(
            [item["image_url"]["url"] for item in request_body["input_references"]],
            ["https://example.com/influencer.jpg", "https://example.com/product.jpg"],
        )
        self.assertIn("Image 1", request_body["prompt"])
        self.assertIn("Image 2", request_body["prompt"])
        self.assertIn("Image 1 as the AI influencer reference", request_body["prompt"])
        self.assertIn("Image 2 as the main product reference", request_body["prompt"])

    def test_submits_all_product_detail_images_after_influencer_and_main_image(self):
        opener = SequentialOpener([
            {"id": "job-1", "polling_url": "https://example.com/poll/job-1"},
            {"id": "job-1", "status": "completed", "unsigned_urls": ["https://example.com/video.mp4"]},
        ])
        client = OpenRouterVideoClient(
            api_key="test-key",
            opener=opener,
            sleeper=lambda _seconds: None,
            image_dimensions_reader=self.image_dimensions_reader,
        )

        client.generate_video(
            VideoGenerationRequest(
                script=SCRIPT,
                image_url="https://example.com/product.jpg",
                influencer_image_url="https://example.com/influencer.jpg",
                detail_image_urls=(
                    "https://example.com/detail-1.jpg",
                    "https://example.com/detail-2.jpg",
                ),
            )
        )

        request_body = json.loads(opener.requests[0].data)
        self.assertEqual(
            [item["image_url"]["url"] for item in request_body["input_references"]],
            [
                "https://example.com/influencer.jpg",
                "https://example.com/product.jpg",
                "https://example.com/detail-1.jpg",
                "https://example.com/detail-2.jpg",
            ],
        )

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
            image_dimensions_reader=self.image_dimensions_reader,
        )

        with self.assertRaisesRegex(VideoGenerationError, "provider failed"):
            client.generate_video(VideoGenerationRequest(script=SCRIPT, image_url="https://example.com/product.jpg"))

    def test_preserves_string_error_when_video_job_fails(self):
        opener = SequentialOpener([
            {"id": "job-1", "polling_url": "https://example.com/poll/job-1", "status": "pending"},
            {"id": "job-1", "status": "FAILED", "error": "provider temporarily unavailable"},
        ])
        client = OpenRouterVideoClient(
            api_key="test-key",
            model="bytedance/seedance-2.0-mini",
            opener=opener,
            sleeper=lambda _seconds: None,
            image_dimensions_reader=self.image_dimensions_reader,
        )

        with self.assertRaisesRegex(
            VideoGenerationError,
            "provider temporarily unavailable",
        ):
            client.generate_video(
                VideoGenerationRequest(
                    script=SCRIPT,
                    image_url="https://example.com/product.jpg",
                )
            )

    def test_falls_back_to_status_when_failed_video_has_no_error_detail(self):
        opener = SequentialOpener([
            {"id": "job-1", "polling_url": "https://example.com/poll/job-1", "status": "pending"},
            {"id": "job-1", "status": "failed"},
        ])
        client = OpenRouterVideoClient(
            api_key="test-key",
            opener=opener,
            sleeper=lambda _seconds: None,
            image_dimensions_reader=self.image_dimensions_reader,
        )

        with self.assertRaisesRegex(VideoGenerationError, "failed"):
            client.generate_video(
                VideoGenerationRequest(
                    script=SCRIPT,
                    image_url="https://example.com/product.jpg",
                )
            )

    def test_uses_last_scene_end_as_video_duration(self):
        opener = SequentialOpener([
            {"id": "job-1", "polling_url": "https://example.com/poll/job-1", "status": "pending"},
            {"id": "job-1", "status": "completed", "unsigned_urls": ["https://example.com/video.mp4"]},
        ])
        client = OpenRouterVideoClient(
            api_key="test-key",
            opener=opener,
            sleeper=lambda _seconds: None,
            image_dimensions_reader=self.image_dimensions_reader,
        )

        client.generate_video(VideoGenerationRequest(script=SCRIPT, image_url="https://example.com/product.jpg"))

        request_body = json.loads(opener.requests[0].data)
        self.assertEqual(request_body["duration"], 8)

    def test_rejects_duration_not_supported_by_selected_model_before_api_call(self):
        script = json.loads(json.dumps(SCRIPT))
        script["scenes"][0]["time_range_sec"] = {"start": 0, "end": 22}
        opener = SequentialOpener([])
        client = OpenRouterVideoClient(
            api_key="test-key",
            opener=opener,
            supported_durations=(4, 6, 8),
            image_dimensions_reader=self.image_dimensions_reader,
        )

        with self.assertRaises(VideoGenerationError):
            client.generate_video(
                VideoGenerationRequest(script=script, image_url="https://example.com/product.jpg")
            )

        self.assertEqual(opener.requests, [])

    def test_rejects_fractional_duration_at_video_provider_boundary(self):
        script = json.loads(json.dumps(SCRIPT))
        script["scenes"][0]["time_range_sec"] = {"start": 0, "end": 2.5}
        opener = SequentialOpener([])
        client = OpenRouterVideoClient(
            api_key="test-key",
            opener=opener,
            supported_durations=(2, 4, 6, 8),
            image_dimensions_reader=self.image_dimensions_reader,
        )

        with self.assertRaises(VideoGenerationError) as context:
            client.generate_video(
                VideoGenerationRequest(script=script, image_url="https://example.com/product.jpg")
            )

        self.assertIn("양의 정수", str(context.exception))
        self.assertEqual(opener.requests, [])

    def test_rejects_invalid_script_before_submitting_video_job(self):
        opener = SequentialOpener([])
        client = OpenRouterVideoClient(
            api_key="test-key",
            opener=opener,
            sleeper=lambda _seconds: None,
            image_dimensions_reader=self.image_dimensions_reader,
        )

        with self.assertRaises(VideoGenerationError):
            client.generate_video(
                VideoGenerationRequest(
                    script={"meta": {"language": "ko"}, "scenes": []},
                    image_url="https://example.com/product.jpg",
                )
            )

        self.assertEqual(opener.requests, [])

    def test_rejects_image_with_dimension_smaller_than_100_before_api_call(self):
        opener = SequentialOpener([])
        client = OpenRouterVideoClient(
            api_key="test-key",
            opener=opener,
            image_dimensions_reader=lambda _image_url: (800, 1),
        )

        with self.assertRaises(VideoGenerationError) as context:
            client.generate_video(
                VideoGenerationRequest(
                    script=SCRIPT,
                    image_url="https://example.com/product.jpg",
                )
            )

        self.assertIn("100px 이상", str(context.exception))
        self.assertEqual(opener.requests, [])

    def test_rejects_small_influencer_image_before_api_call(self):
        opener = SequentialOpener([])

        def read_dimensions(image_url):
            return (720, 1280) if "product" in image_url else (99, 500)

        client = OpenRouterVideoClient(
            api_key="test-key",
            opener=opener,
            image_dimensions_reader=read_dimensions,
        )

        with self.assertRaises(VideoGenerationError):
            client.generate_video(
                VideoGenerationRequest(
                    script=SCRIPT,
                    image_url="https://example.com/product.jpg",
                    influencer_image_url="https://example.com/influencer.jpg",
                )
            )

        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
