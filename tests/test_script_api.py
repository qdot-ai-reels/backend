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
    @patch("app.api.v1.script.OpenRouterClient.from_env")
    def test_generates_script_from_http_body(self, from_env):
        client = FakeScriptClient()
        from_env.return_value = client
        body = ScriptGenerationBody(
            product={"brand_name": "프랭클린", "product_name": "아기 주방세제"}
        )

        result = generate_script(body)

        self.assertEqual(result["summary"]["key_message"], "메시지")
        self.assertEqual(client.last_request.product["brand_name"], "프랭클린")


if __name__ == "__main__":
    unittest.main()
