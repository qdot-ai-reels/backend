import unittest
from unittest.mock import patch

from app.api.v1.script import ScriptGenerationBody, generate_script


class FakeScriptClient:
    def __init__(self):
        self.last_request = None

    def generate_script(self, request):
        self.last_request = request
        return {
            "meta": {"aspect_ratio": "9:16", "max_duration_sec": request.max_duration_seconds},
            "summary": "테스트 스크립트",
            "scenes": [
                {
                    "scene_number": 1,
                    "time_range_sec": [0, 3],
                    "visual": "상품을 보여준다.",
                    "subtitle": "상품 소개",
                    "voiceover": "상품을 소개합니다.",
                    "intent": "hook",
                }
            ],
            "compliance_notes": [],
        }


class ScriptApiTests(unittest.TestCase):
    @patch("app.api.v1.script.OpenRouterClient.from_env")
    def test_generates_script_from_http_body(self, from_env):
        client = FakeScriptClient()
        from_env.return_value = client
        body = ScriptGenerationBody(
            product={"brand_name": "프랭클린", "product_name": "아기 주방세제"}
        )

        result = generate_script(body)

        self.assertEqual(result["summary"], "테스트 스크립트")
        self.assertEqual(client.last_request.product["brand_name"], "프랭클린")


if __name__ == "__main__":
    unittest.main()
