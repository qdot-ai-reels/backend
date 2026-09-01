import unittest
from unittest.mock import patch

from app.api.v1.script import ScriptGenerationBody, generate_script


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
    @patch("app.api.v1.script.build_script_client")
    @patch("app.api.v1.script.validate_product_image_inputs")
    def test_generates_script_from_http_body(self, _validate_images, build_client):
        client = FakeScriptClient()
        build_client.return_value = client
        body = ScriptGenerationBody(
            product={"brand_name": "프랭클린", "product_name": "아기 주방세제"},
            image_url="https://example.com/product.jpg",
            reviews=["거품이 잘 납니다."],
            prompt="30초 이내 광고로 작성",
        )

        result = generate_script(body)

        self.assertEqual(result["summary"]["key_message"], "메시지")
        self.assertEqual(client.last_request.product["brand_name"], "프랭클린")
        self.assertEqual(client.last_request.image_url, "https://example.com/product.jpg")
        self.assertEqual(client.last_request.reviews, ["거품이 잘 납니다."])
        self.assertEqual(client.last_request.custom_prompt, "30초 이내 광고로 작성")

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
            generate_script(body)

        build_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
