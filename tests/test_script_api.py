import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks

from app.api.v1.script import ScriptGenerationBody, generate_script, run_script_job


class FakeScriptClient:
    def __init__(self):
        self.last_request = None

    def generate_script(self, request):
        self.last_request = request
        return {
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
                    "time_range_sec": {"start": 0, "end": 3},
                    "visual": "상품을 보여준다.",
                    "auditory": {
                        "subtitle": "상품 소개",
                        "voiceover": "상품을 소개합니다.",
                    },
                    "notes": "상품 소개",
                }
            ],
            "compliance_notes": {"avoid": [], "focus": []},
        }


class ScriptApiTests(unittest.TestCase):
    @patch("app.api.v1.script.create_job")
    @patch("app.api.v1.script.validate_product_image_inputs")
    def test_queues_script_generation_from_http_body(self, _validate_images, create_job):
        class FakeBackgroundTasks:
            def __init__(self):
                self.task = None

            def add_task(self, function, *args, **kwargs):
                self.task = (function, args, kwargs)

        background_tasks = FakeBackgroundTasks()
        client = FakeScriptClient()
        body = ScriptGenerationBody(
            product={"brand_name": "프랭클린", "product_name": "아기 주방세제"},
            image_url="https://example.com/product.jpg",
            reviews=["거품이 잘 납니다."],
            prompt="30초 이내 광고로 작성",
        )

        result = generate_script(body, background_tasks)

        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(result["status_url"], f"/api/v1/reels/script/{result['job_id']}")
        create_job.assert_called_once()
        self.assertEqual(background_tasks.task[0], run_script_job)

    @patch("app.api.v1.script.update_job")
    @patch("app.api.v1.script.build_script_client")
    @patch("app.api.v1.script.validate_product_image_inputs")
    def test_script_job_stores_completed_script(self, _validate_images, build_client, update_job):
        client = FakeScriptClient()
        build_client.return_value = client
        run_script_job(
            "script-job",
            {
                "product": {"brand_name": "프랭클린", "product_name": "아기 주방세제"},
                "image_url": "https://example.com/product.jpg",
                "reviews": ["거품이 잘 납니다."],
                "prompt": "30초 이내 광고로 작성",
                "max_duration_seconds": 15,
            },
        )

        self.assertEqual(client.last_request.product["brand_name"], "프랭클린")
        self.assertEqual(client.last_request.image_url, "https://example.com/product.jpg")
        self.assertEqual(client.last_request.reviews, ["거품이 잘 납니다."])
        self.assertEqual(client.last_request.custom_prompt, "30초 이내 광고로 작성")
        self.assertEqual(update_job.call_args.kwargs["status"], "COMPLETED")

    @patch("app.api.v1.script.validate_product_image_inputs")
    @patch("app.api.v1.script.build_script_client")
    def test_rejects_invalid_product_image_before_openrouter_request(
        self, build_client, validate_images
    ):
        validate_images.side_effect = ValueError(
            "상품 상세 이미지 1번째가 너무 작습니다: 860x1"
        )
        body = ScriptGenerationBody(
            product={"image_url": "https://example.com/product.jpg"},
        )

        with self.assertRaisesRegex(Exception, "상품 상세 이미지 1번째"):
            generate_script(body, BackgroundTasks())

        build_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
