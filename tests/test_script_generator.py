import json
import os
import unittest
from unittest.mock import patch
from app.script_generator import (
    MAX_SCRIPT_DURATION_SECONDS,
    OpenRouterClient,
    OpenRouterConfigurationError,
    ScriptGenerationRequest,
    ScriptValidationError,
    build_script_prompt,
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
    "meta": {
        "output_format_version": "1.0",
        "framework": "Hook-Body-CTA",
        "language": "ko",
    },
    "summary": {
        "main_target": "아기 식기를 사용하는 보호자",
        "pain_point": "성분이 걱정되는 보호자",
        "product_usp": "비건 인증",
        "key_message": "성분을 확인하고 사용하세요.",
        "tone_and_manner": "차분한 생활형 광고",
    },
    "scenes": [
        {
            "scene_name": "Hook",
            "time_range_sec": {"start": 0, "end": 3},
            "visual": "제품을 화면 중앙에 보여준다.",
            "auditory": {
                "subtitle": "아기 식기 세제",
                "voiceover": "성분 확인하세요.",
            },
            "notes": "제품을 먼저 보여준다.",
        }
    ],
    "compliance_notes": {
        "avoid": ["상품 정보에 없는 효능"],
        "focus": ["제공된 상품 정보"],
    },
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


class ScriptGeneratorTests(unittest.TestCase):
    def test_rejects_script_request_above_product_maximum_duration(self):
        with self.assertRaises(ValueError):
            ScriptGenerationRequest(
                product=PRODUCT,
                max_duration_seconds=MAX_SCRIPT_DURATION_SECONDS + 1,
            )

    def test_extracts_json_from_markdown_code_fence(self):
        content = f"```json\n{json.dumps(VALID_DOCUMENT, ensure_ascii=False)}\n```"

        self.assertEqual(extract_script_json(content), VALID_DOCUMENT)

    def test_rejects_script_without_scenes(self):
        invalid_document = {"meta": {}, "summary": "내용", "scenes": []}

        with self.assertRaises(ScriptValidationError):
            validate_script_document(invalid_document)

    def test_rejects_legacy_script_output_shape(self):
        legacy_document = {
            "meta": {"aspect_ratio": "9:16", "max_duration_sec": 30},
            "summary": "기존 형식",
            "scenes": [{"time_range_sec": [0, 3]}],
            "compliance_notes": [],
        }

        with self.assertRaises(ScriptValidationError):
            validate_script_document(legacy_document)

    def test_rejects_script_without_output_format_metadata(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        del invalid_document["meta"]["language"]

        with self.assertRaises(ScriptValidationError):
            validate_script_document(invalid_document)

    def test_rejects_scene_with_invalid_time_range(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        invalid_document["scenes"][0]["time_range_sec"] = {"start": 3, "end": 1}

        with self.assertRaises(ScriptValidationError):
            validate_script_document(invalid_document)

    def test_rejects_scene_ending_after_requested_max_duration(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        invalid_document["scenes"][0]["time_range_sec"] = {"start": 0, "end": 31}

        with self.assertRaises(ScriptValidationError):
            validate_script_document(invalid_document, max_duration_seconds=30)

    def test_rejects_scene_dialogue_longer_than_time_range(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        invalid_document["scenes"][0]["time_range_sec"] = {"start": 0, "end": 1}
        invalid_document["scenes"][0]["auditory"]["voiceover"] = "이 장면에서 읽기에는 너무 긴 광고 대사입니다."

        with self.assertRaises(ScriptValidationError):
            validate_script_document(invalid_document)

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
        self.assertIn("4.5음절", body["messages"][0]["content"])

    def test_prompt_uses_structured_output_without_video_settings_in_meta(self):
        prompt = build_script_prompt(ScriptGenerationRequest(product=PRODUCT))

        self.assertIn('"output_format_version"', prompt)
        self.assertIn('"time_range_sec": {"start": 0, "end": 3}', prompt)
        self.assertIn('"auditory"', prompt)
        self.assertNotIn('"aspect_ratio": "9:16"', prompt)
        self.assertNotIn('"max_duration_sec": 30', prompt)

    def test_asks_model_to_derive_missing_usp_without_mutating_product(self):
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
            return FakeResponse(response_payload)

        product = {**PRODUCT, "usp": None}
        client = OpenRouterClient(
            api_key="test-key",
            model="openai/gpt-oss-20b:free",
            opener=fake_opener,
        )
        client.generate_script(ScriptGenerationRequest(product=product))

        prompt = json.loads(captured["request"].data)["messages"][0]["content"]
        self.assertIn("USP가 비어있거나 null인 경우", prompt)
        self.assertIn("상품 데이터의 다른 정보만을 바탕으로", prompt)
        self.assertIsNone(product["usp"])

    def test_does_not_ask_to_derive_usp_when_product_has_usp(self):
        request = ScriptGenerationRequest(product={**PRODUCT, "usp": "안심 세척"})

        prompt = build_script_prompt(request)

        self.assertNotIn("USP가 비어있거나 null인 경우", prompt)
        self.assertIn("안심 세척", prompt)

    def test_asks_to_derive_usp_when_product_usp_is_only_whitespace(self):
        request = ScriptGenerationRequest(product={**PRODUCT, "usp": "   "})

        prompt = build_script_prompt(request)

        self.assertIn("USP가 비어있거나 null인 경우", prompt)

    def test_accepts_null_voiceover_but_requires_script_output_fields(self):
        document = json.loads(json.dumps(VALID_DOCUMENT))
        document["scenes"][0]["auditory"]["voiceover"] = None

        self.assertEqual(validate_script_document(document), document)

        missing_field = json.loads(json.dumps(document))
        del missing_field["scenes"][0]["auditory"]["voiceover"]
        with self.assertRaises(ScriptValidationError):
            validate_script_document(missing_field)

    def test_excludes_social_posts_from_product_prompt(self):
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
            return FakeResponse(response_payload)

        product = {
            **PRODUCT,
            "social_posts": [{"content": "사용자 게시물"}],
        }
        client = OpenRouterClient(
            api_key="test-key",
            model="openai/gpt-oss-20b:free",
            opener=fake_opener,
        )

        client.generate_script(ScriptGenerationRequest(product=product))

        prompt = json.loads(captured["request"].data)["messages"][0]["content"]
        self.assertNotIn("social_posts", prompt)
        self.assertNotIn("사용자 게시물", prompt)

    def test_rejects_openrouter_response_without_content(self):
        client = OpenRouterClient(
            api_key="test-key",
            model="test-model",
            opener=lambda _request, timeout: FakeResponse({"choices": []}),
        )

        with self.assertRaises(ScriptValidationError):
            client.generate_script(ScriptGenerationRequest(product=PRODUCT))

    def test_retries_with_fallback_model_after_schema_failure(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        del invalid_document["meta"]["framework"]
        opener = SequentialOpener([
            {"choices": [{"message": {"content": json.dumps(invalid_document)}}]},
            {"choices": [{"message": {"content": json.dumps(VALID_DOCUMENT)}}]},
        ])
        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/free",
            fallback_model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            opener=opener,
        )

        result = client.generate_script(ScriptGenerationRequest(product=PRODUCT))

        self.assertEqual(result, VALID_DOCUMENT)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(json.loads(opener.requests[0].data)["model"], "openrouter/free")
        self.assertEqual(
            json.loads(opener.requests[1].data)["model"],
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        )

    def test_retries_with_fallback_model_after_dialogue_length_failure(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        invalid_document["scenes"][0]["time_range_sec"] = {"start": 0, "end": 1}
        invalid_document["scenes"][0]["auditory"]["voiceover"] = "이 장면에서 읽기에는 너무 긴 광고 대사입니다."
        opener = SequentialOpener([
            {"choices": [{"message": {"content": json.dumps(invalid_document)}}]},
            {"choices": [{"message": {"content": json.dumps(VALID_DOCUMENT)}}]},
        ])
        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/free",
            fallback_model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            opener=opener,
        )

        result = client.generate_script(ScriptGenerationRequest(product=PRODUCT))

        self.assertEqual(result, VALID_DOCUMENT)
        self.assertEqual(len(opener.requests), 2)

    def test_retries_by_requesting_a_new_complete_script_after_dialogue_failure(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        invalid_document["scenes"][0]["time_range_sec"] = {"start": 0, "end": 1}
        invalid_document["scenes"][0]["auditory"]["voiceover"] = "이 장면에서 읽기에는 너무 긴 광고 대사입니다."
        opener = SequentialOpener([
            {"choices": [{"message": {"content": json.dumps(invalid_document)}}]},
            {"choices": [{"message": {"content": json.dumps(VALID_DOCUMENT)}}]},
        ])
        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/free",
            fallback_model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            opener=opener,
        )

        client.generate_script(ScriptGenerationRequest(product=PRODUCT))

        first_prompt = json.loads(opener.requests[0].data)["messages"][0]["content"]
        second_prompt = json.loads(opener.requests[1].data)["messages"][0]["content"]
        self.assertIn('"scenes"', first_prompt)
        self.assertIn('"scenes"', second_prompt)
        self.assertIn("이전 모델 응답이 형식 검증에 실패했습니다", second_prompt)


if __name__ == "__main__":
    unittest.main()
