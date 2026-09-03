import json
from io import BytesIO
import os
import unittest
from urllib.error import HTTPError
from unittest.mock import patch
from app.script_generator import (
    MAX_SCRIPT_DURATION_SECONDS,
    OpenRouterClient,
    OpenRouterConfigurationError,
    OpenRouterRequestError,
    ScriptGenerationRequest,
    select_supported_video_duration,
    ScriptValidationError,
    SCRIPT_RESPONSE_SCHEMA,
    build_script_prompt,
    build_script_message_content,
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
        "language": "ko",
    },
    "product": {
        "usp": "비건 인증",
    },
    "customer": {
        "main_target": "아기 식기를 사용하는 보호자",
        "pain_point": "성분이 걱정되는 보호자",
    },
    "ads": {
        "goal": "상품 정보 전달",
        "cta_action": "상품 확인",
        "channel_platform": "Instagram Reels",
        "ad_planner": {"persona": None},
        "speaker": {"persona": None, "tone": "차분한 말투"},
        "main_target": "아기 식기를 사용하는 보호자",
    },
    "video": {
        "video_duration": "3",
        "required_scenes_elements": None,
        "forbidden_scenes_elements": None,
    },
    "scenes": [
        {
            "section": "Hook",
            "time_range_sec": {"start": 0, "end": 3},
            "visual": "제품을 화면 중앙에 보여준다.",
            "auditory": {
                "subtitle": "아기 식기 세제",
                "voiceover": "성분 확인하세요.",
            },
            "intent": "제품을 먼저 보여준다.",
            "notes": "제품을 먼저 보여준다.",
        }
    ],
    "etc": {
        "additional_information": None,
        "video_ads_methodology": "Hook-Body-CTA",
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


class ErrorThenSuccessOpener:
    def __init__(self, status_code, reason, detail, success_payload):
        self.status_code = status_code
        self.reason = reason
        self.detail = detail
        self.success_payload = success_payload
        self.calls = 0

    def __call__(self, request, timeout):
        self.calls += 1
        if self.calls == 1:
            body = json.dumps({"error": {"message": self.detail}}).encode("utf-8")
            raise HTTPError(
                request.full_url,
                self.status_code,
                self.reason,
                {},
                BytesIO(body),
            )
        return FakeResponse(self.success_payload)


class ScriptGeneratorTests(unittest.TestCase):
    def test_retries_when_provider_returns_400_error(self):
        opener = ErrorThenSuccessOpener(
            400,
            "Bad Request",
            "Provider returned error",
            {"choices": [{"message": {"content": json.dumps(VALID_DOCUMENT)}}]},
        )
        sleeps = []
        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/test",
            fallback_model=None,
            opener=opener,
            sleep=lambda seconds: sleeps.append(seconds),
        )

        result = client.generate_script(ScriptGenerationRequest(product=PRODUCT))

        self.assertEqual(result, VALID_DOCUMENT)
        self.assertEqual(opener.calls, 2)
        self.assertEqual(sleeps, [2.0])

    def test_retries_when_no_image_endpoint_is_available(self):
        opener = ErrorThenSuccessOpener(
            404,
            "Not Found",
            "No endpoints available matching your guardrail restrictions and data policy",
            {"choices": [{"message": {"content": json.dumps(VALID_DOCUMENT)}}]},
        )
        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/test",
            fallback_model=None,
            opener=opener,
            sleep=lambda _seconds: None,
        )

        result = client.generate_script(ScriptGenerationRequest(product=PRODUCT))

        self.assertEqual(result, VALID_DOCUMENT)
        self.assertEqual(opener.calls, 2)

    def test_does_not_retry_unrelated_400_error(self):
        body = json.dumps({"error": {"code": "invalid_model", "message": "model is unavailable"}}).encode(
            "utf-8"
        )

        def failing_opener(request, timeout):
            raise HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(body))

        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/test",
            fallback_model=None,
            opener=failing_opener,
            sleep=lambda _seconds: None,
        )

        with self.assertRaisesRegex(OpenRouterRequestError, "HTTP 400"):
            client.generate_script(ScriptGenerationRequest(product=PRODUCT))
    def test_selects_longest_supported_duration_under_configured_cap(self):
        self.assertEqual(select_supported_video_duration(15, (5, 10)), 10)
        self.assertEqual(select_supported_video_duration(8, (5, 10)), 5)

    def test_rejects_cap_below_all_supported_durations(self):
        with self.assertRaisesRegex(ValueError, "사용할 수 있는 모델 지원 길이가 없습니다"):
            select_supported_video_duration(3, (5, 10))

    def test_includes_safe_provider_detail_for_http_errors(self):
        body = json.dumps(
            {"error": {"code": "invalid_model", "message": "model is unavailable", "type": "provider"}}
        ).encode("utf-8")

        def failing_opener(_request, timeout):
            raise HTTPError("https://openrouter.ai", 403, "Forbidden", {}, BytesIO(body))

        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/test",
            fallback_model=None,
            max_attempts=1,
            opener=failing_opener,
        )

        with self.assertRaisesRegex(
            OpenRouterRequestError,
            r"HTTP 403: code=invalid_model, message=model is unavailable, type=provider",
        ):
            client.generate_script(ScriptGenerationRequest(product=PRODUCT))

    def test_logs_provider_diagnostics_without_request_content(self):
        body = json.dumps(
            {
                "error": {
                    "code": "no_endpoints",
                    "message": "No endpoints available matching your guardrail restrictions",
                }
            }
        ).encode("utf-8")

        def failing_opener(request, timeout):
            raise HTTPError(
                request.full_url,
                404,
                "Not Found",
                {"x-request-id": "request-123"},
                BytesIO(body),
            )

        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/test",
            fallback_model=None,
            max_attempts=1,
            opener=failing_opener,
        )

        with self.assertLogs("app.script_generator", level="WARNING") as captured:
            with self.assertRaises(OpenRouterRequestError):
                client.generate_script(
                    ScriptGenerationRequest(
                        product=PRODUCT,
                        image_url="https://example.com/product.jpg",
                    )
                )

        message = "\n".join(captured.output)
        self.assertIn("model=openrouter/test", message)
        self.assertIn("attempt=1", message)
        self.assertIn("image_included=True", message)
        self.assertIn("request_id=request-123", message)
        self.assertIn("no_endpoints", message)
        self.assertNotIn("test-key", message)
        self.assertNotIn("product.jpg", message)

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

    def test_uses_free_default_model_when_script_model_environment_variable_is_blank(self):
        with patch.dict(
            os.environ,
            {"OPENROUTER_SCRIPT_MODEL": "", "OPENROUTER_FALLBACK_MODEL": ""},
            clear=False,
        ):
            client = OpenRouterClient.from_env()

        self.assertEqual(client.model, "openai/gpt-oss-20b:free")
        self.assertEqual(client.fallback_model, client.model)

    def test_defaults_fallback_model_to_configured_script_model(self):
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_SCRIPT_MODEL": "openai/gpt-5.6-luna",
                "OPENROUTER_FALLBACK_MODEL": "",
            },
            clear=False,
        ):
            client = OpenRouterClient.from_env()

        self.assertEqual(client.fallback_model, "openai/gpt-5.6-luna")

    def test_preserves_explicit_fallback_model(self):
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_SCRIPT_MODEL": "openai/gpt-5.6-luna",
                "OPENROUTER_FALLBACK_MODEL": "openai/gpt-5.4",
            },
            clear=False,
        ):
            client = OpenRouterClient.from_env()

        self.assertEqual(client.fallback_model, "openai/gpt-5.4")

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
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(body["response_format"]["json_schema"]["name"], "reels_script")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertIn("scenes", body["response_format"]["json_schema"]["schema"]["required"])
        self.assertIn("product", body["response_format"]["json_schema"]["schema"]["required"])
        self.assertNotIn("summary", body["response_format"]["json_schema"]["schema"]["required"])
        self.assertIn("Selling Point", body["messages"][0]["content"])
        self.assertIn("EWG 그린등급", body["messages"][0]["content"])
        self.assertIn("4.5음절", body["messages"][0]["content"])

    def test_prompt_matches_colab_and_keeps_schema_out_of_prompt(self):
        prompt = build_script_prompt(ScriptGenerationRequest(product=PRODUCT))

        self.assertIn("### Condition", prompt)
        self.assertIn("### Methodology", prompt)
        self.assertIn("### 상품 정보", prompt)
        self.assertNotIn("다음 JSON 객체만 반환하세요", prompt)
        self.assertNotIn('"output_format_version"', prompt)

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
        self.assertIn("USP(Unique Selling Point)값이 null이면", prompt)
        self.assertIn("상품정보 항목의 내용에 근거하여", prompt)
        self.assertIsNone(product["usp"])

    def test_does_not_ask_to_derive_usp_when_product_has_usp(self):
        request = ScriptGenerationRequest(product={**PRODUCT, "usp": "안심 세척"})

        prompt = build_script_prompt(request)

        self.assertIn("USP(Unique Selling Point)값이 null이면", prompt)
        self.assertIn("입력한 그대로 출력할 것", prompt)
        self.assertIn("안심 세척", prompt)

    def test_asks_to_derive_usp_when_product_usp_is_only_whitespace(self):
        request = ScriptGenerationRequest(product={**PRODUCT, "usp": "   "})

        prompt = build_script_prompt(request)

        self.assertIn("USP(Unique Selling Point)값이 null이면", prompt)

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

    def test_includes_reviews_and_custom_prompt_in_script_prompt(self):
        request = ScriptGenerationRequest(
            product=PRODUCT,
            reviews=["거품이 잘 납니다."],
            custom_prompt="30초 이내 광고로 작성",
        )

        prompt = build_script_prompt(request)

        self.assertIn("거품이 잘 납니다.", prompt)
        self.assertNotIn("30초 이내 광고로 작성", prompt)

    def test_includes_notion_product_fields_as_labeled_prompt_context(self):
        prompt = build_script_prompt(
            ScriptGenerationRequest(
                product={
                    **PRODUCT,
                    "selling_point": "식물 유래 성분",
                    "usp": "비건 인증",
                    "curator_pitch": "신뢰감 있는 제품",
                    "hashtags": ["아기세제"],
                    "description_text": "상품 설명",
                    "detail_info": "상세 정보",
                    "reviews": ["리뷰 내용"],
                },
                custom_prompt="광고 목적: 판매\nCTA: 링크 확인",
            )
        )

        self.assertIn("- Selling Point: 식물 유래 성분", prompt)
        self.assertIn("- USP(Unique Selling Point): 비건 인증", prompt)
        self.assertIn("- Curator Pitch: 신뢰감 있는 제품", prompt)
        self.assertIn("- Hashtags: ['아기세제']", prompt)
        self.assertIn("- Description Text: 상품 설명", prompt)
        self.assertIn("- Detail Info: 상세 정보", prompt)
        self.assertIn("- Reviews: ['리뷰 내용']", prompt)
        self.assertIn("- CTA Action: 링크 확인", prompt)

    def test_includes_prompt_filling_rule_and_ad_methodologies(self):
        prompt = build_script_prompt(
            ScriptGenerationRequest(product=PRODUCT, custom_prompt="리필 여부를 확인해 반영")
        )

        self.assertIn("유저가 프롬프트를 통해 해당 상품정보를 입력해주었다면", prompt)
        self.assertIn("Hook-Body-CTA", prompt)
        self.assertIn("PAS", prompt)
        self.assertIn("Anti-Slop Prompt For Video", prompt)

    def test_includes_all_script_prompt_260830_1_video_direction_rules(self):
        prompt = build_script_prompt(ScriptGenerationRequest(product=PRODUCT))

        self.assertIn("dolly", prompt)
        self.assertIn("Reduce fill", prompt)
        self.assertIn(
            "Anti-Slop Prompt For Video: 현실성 있는 영상을 위해 불완전성(imperfection)을 더하라",
            prompt,
        )
        self.assertIn("카메라를 주시하며 말하지 않는다", prompt)
        self.assertIn("같은 인물의 얼굴, 헤어스타일, 의상이 장면마다 유지", prompt)
        self.assertIn("상품 라벨의 글자와 로고는 식별 가능한 정면 클로즈업으로 보여주지 않는다", prompt)
        self.assertIn("자막, 가격, 할인율, CTA 문구는 영상에 삽입하지 않는다", prompt)

    def test_includes_all_260830_1_content_rules(self):
        prompt = build_script_prompt(ScriptGenerationRequest(product=PRODUCT))

        expected_rules = (
            "첫 1~3초 안에",
            "어떤 상황에서 왜 좋은지",
            "소비자가 판단할 수 있는 정보",
            "지나치게 과장하지 말아야",
            "허위 경험",
            "slight handheld motion",
            "imperfect skin texture",
            "subtle blemishes",
            "wrinkled fabric",
            "natural and subtle asymmetry",
        )
        for rule in expected_rules:
            self.assertIn(rule, prompt)

    def test_enforces_documented_visual_length_in_response_schema(self):
        visual_schema = SCRIPT_RESPONSE_SCHEMA["properties"]["scenes"]["items"]["properties"]["visual"]

        self.assertEqual(visual_schema["maxLength"], 99)

    def test_rejects_visual_that_reaches_100_characters(self):
        document = json.loads(json.dumps(VALID_DOCUMENT))
        document["scenes"][0]["visual"] = "가" * 100

        with self.assertRaisesRegex(ScriptValidationError, "visual"):
            validate_script_document(document)

    def test_adds_product_image_to_multimodal_message(self):
        request = ScriptGenerationRequest(
            product=PRODUCT,
            image_url="https://example.com/product.jpg",
        )

        content = build_script_message_content(request, "상품 스크립트를 작성하세요.")

        self.assertEqual(content[0], {"type": "text", "text": "상품 스크립트를 작성하세요."})
        self.assertEqual(
            content[1],
            {
                "type": "image_url",
                "image_url": {"url": "https://example.com/product.jpg"},
            },
        )

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
        del invalid_document["meta"]["language"]
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
        self.assertNotIn("다음 JSON 객체만 반환하세요", first_prompt)
        self.assertNotIn('"scenes"', first_prompt)
        self.assertNotIn("다음 JSON 객체만 반환하세요", second_prompt)
        self.assertNotIn('"scenes"', second_prompt)
        self.assertEqual(first_prompt, second_prompt)

    def test_honors_configured_attempts_even_after_fallback_model_is_used(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        del invalid_document["meta"]["language"]
        opener = SequentialOpener([
            {"choices": [{"message": {"content": json.dumps(invalid_document)}}]},
            {"choices": [{"message": {"content": json.dumps(invalid_document)}}]},
            {"choices": [{"message": {"content": json.dumps(VALID_DOCUMENT)}}]},
        ])
        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/free",
            fallback_model="fallback/model",
            max_attempts=3,
            opener=opener,
        )

        result = client.generate_script(ScriptGenerationRequest(product=PRODUCT))

        self.assertEqual(result, VALID_DOCUMENT)
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(
            [json.loads(request.data)["model"] for request in opener.requests],
            ["openrouter/free", "fallback/model", "fallback/model"],
        )

    def test_default_attempts_are_five(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        del invalid_document["meta"]["language"]
        opener = SequentialOpener(
            [{"choices": [{"message": {"content": json.dumps(invalid_document)}}]}] * 4
            + [{"choices": [{"message": {"content": json.dumps(VALID_DOCUMENT)}}]}]
        )
        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/free",
            fallback_model=None,
            opener=opener,
            retry_delay_seconds=0,
        )

        result = client.generate_script(ScriptGenerationRequest(product=PRODUCT))

        self.assertEqual(result, VALID_DOCUMENT)
        self.assertEqual(len(opener.requests), 5)

    def test_retry_prompt_remains_identical_to_the_initial_prompt(self):
        invalid_document = json.loads(json.dumps(VALID_DOCUMENT))
        invalid_document["scenes"][0]["time_range_sec"] = {"start": 0, "end": 1}
        invalid_document["scenes"][0]["auditory"]["voiceover"] = (
            "이 장면에서 읽기에는 너무 긴 광고 대사입니다."
        )
        opener = SequentialOpener([
            {"choices": [{"message": {"content": json.dumps(invalid_document)}}]},
            {"choices": [{"message": {"content": json.dumps(VALID_DOCUMENT)}}]},
        ])
        client = OpenRouterClient(
            api_key="test-key",
            model="openrouter/free",
            fallback_model="fallback/model",
            opener=opener,
        )

        client.generate_script(ScriptGenerationRequest(product=PRODUCT))

        first_prompt = json.loads(opener.requests[0].data)["messages"][0]["content"]
        retry_prompt = json.loads(opener.requests[1].data)["messages"][0]["content"]
        self.assertEqual(first_prompt, retry_prompt)


if __name__ == "__main__":
    unittest.main()
