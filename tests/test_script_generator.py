import json
import os
import unittest
from unittest.mock import patch
from app.script_generator import (
    OpenRouterClient,
    OpenRouterConfigurationError,
    ScriptGenerationRequest,
    ScriptValidationError,
    extract_script_json,
    validate_script_document,
)


PRODUCT = {
    "brand_name": "프랭클린",
    "product_name": "아기 주방세제",
    "price": 22900,
    "discount_rate": 49,
    "selling_points": ["EWG 그린등급", "비건 인증"],
    "image_url": "https://example.com/product.jpg",
}


VALID_DOCUMENT = {
    "meta": {"aspect_ratio": "9:16", "max_duration_sec": 30},
    "summary": "아기 식기를 위한 주방세제를 소개합니다.",
    "scenes": [
        {
            "scene_number": 1,
            "time_range_sec": [0, 3],
            "visual": "제품을 화면 중앙에 보여준다.",
            "subtitle": "아기 식기 세제",
            "voiceover": "아기 식기 세제, 성분부터 확인해 보세요.",
            "intent": "hook",
        }
    ],
    "compliance_notes": ["제공된 상품 정보만 사용"],
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


class ScriptGeneratorTests(unittest.TestCase):
    def test_extracts_json_from_markdown_code_fence(self):
        content = f"```json\n{json.dumps(VALID_DOCUMENT, ensure_ascii=False)}\n```"

        self.assertEqual(extract_script_json(content), VALID_DOCUMENT)

    def test_rejects_script_without_scenes(self):
        invalid_document = {"meta": {}, "summary": "내용", "scenes": []}

        with self.assertRaises(ScriptValidationError):
            validate_script_document(invalid_document)

    def test_rejects_script_with_wrong_aspect_ratio(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        invalid_document["meta"]["aspect_ratio"] = "9:12"

        with self.assertRaises(ScriptValidationError):
            validate_script_document(invalid_document)

    def test_rejects_scene_with_invalid_time_range(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        invalid_document["scenes"][0]["time_range_sec"] = [3, 1]

        with self.assertRaises(ScriptValidationError):
            validate_script_document(invalid_document)

    def test_rejects_scene_ending_after_requested_max_duration(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        invalid_document["scenes"][0]["time_range_sec"] = [0, 31]

        with self.assertRaises(ScriptValidationError):
            validate_script_document(invalid_document, max_duration_seconds=30)

    def test_requires_api_key_before_calling_openrouter(self):
        client = OpenRouterClient(api_key="", model="openai/gpt-oss-20b:free")

        with self.assertRaises(OpenRouterConfigurationError):
            client.generate_script(ScriptGenerationRequest(product=PRODUCT))

    def test_uses_free_default_model_when_model_environment_variable_is_blank(self):
        with patch.dict(os.environ, {"OPENROUTER_MODEL": ""}, clear=False):
            client = OpenRouterClient.from_env()

        self.assertEqual(client.model, "openai/gpt-oss-20b:free")

    def test_sends_product_prompt_and_parses_openrouter_response(self):
        response_payload = {
            "choices": [{
                "message": {
                    "content": json.dumps(VALID_DOCUMENT, ensure_ascii=False)
                }
            }]
        }
        captured = {}

        def fake_opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(response_payload)

        client = OpenRouterClient(
            api_key="test-key",
            model="openai/gpt-oss-20b:free",
            opener=fake_opener,
        )
        result = client.generate_script(ScriptGenerationRequest(product=PRODUCT))

        self.assertEqual(result, VALID_DOCUMENT)
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer test-key")
        body = json.loads(captured["request"].data)
        self.assertEqual(body["model"], "openai/gpt-oss-20b:free")
        self.assertEqual(body["reasoning"], {"exclude": True})
        self.assertIn("프랭클린", body["messages"][0]["content"])

    def test_rejects_openrouter_response_without_content(self):
        client = OpenRouterClient(
            api_key="test-key",
            model="test-model",
            opener=lambda _request, timeout: FakeResponse({"choices": []}),
        )

        with self.assertRaises(ScriptValidationError):
            client.generate_script(ScriptGenerationRequest(product=PRODUCT))


if __name__ == "__main__":
    unittest.main()
