import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.api.v1.final_generation import get_generation_status
from app.generation_jobs import _error_metadata, get_job


class FakeSession:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, _model, _job_id):
        return self.row


def job_row(payload_json):
    return SimpleNamespace(
        job_id="job-1",
        status="PENDING",
        stage="QUEUED",
        input_type="product_and_script",
        script_json=None,
        payload_json=payload_json,
        video_job_id=None,
        caption_job_id=None,
        output_path=None,
        error_message=None,
        cost=None,
        candidate_count=0,
        candidates_json="[]",
        created_at=None,
        updated_at=None,
    )


class GenerationJobProvenanceTests(unittest.TestCase):
    def get_job_for_payload(self, payload):
        raw_payload = json.dumps(payload) if payload is not None else None
        with patch(
            "app.generation_jobs.SessionLocal",
            return_value=FakeSession(job_row(raw_payload)),
        ):
            return get_job("job-1")

    def test_product_only_payload_exposes_safe_zero_count(self):
        result = self.get_job_for_payload(
            {"image_url": "https://private.example/product-secret.jpg"}
        )

        self.assertEqual(result["visual_mode"], "product_only")
        self.assertEqual(result["influencer_reference_count"], 0)
        self.assertNotIn("private.example", json.dumps(result))
        self.assertNotIn("payload", result)

    def test_generated_model_provenance_is_preserved_without_reference(self):
        result = self.get_job_for_payload(
            {
                "visual_mode": "generated_model",
                "image_url": "https://private.example/product-secret.jpg",
            }
        )

        self.assertEqual(result["visual_mode"], "generated_model")
        self.assertEqual(result["influencer_reference_count"], 0)
        self.assertNotIn("private.example", json.dumps(result))

    def test_explicit_influencer_references_are_deduplicated_and_capped(self):
        result = self.get_job_for_payload(
            {
                "influencer_image_urls": [
                    "https://private.example/person-a.jpg",
                    "https://private.example/person-a.jpg",
                    "https://private.example/person-b.jpg",
                    "https://private.example/person-c.jpg",
                ]
            }
        )

        self.assertEqual(result["visual_mode"], "model_included")
        self.assertEqual(result["influencer_reference_count"], 2)
        self.assertNotIn("person-a", json.dumps(result))

    def test_legacy_singular_reference_is_supported(self):
        result = self.get_job_for_payload(
            {"influencer_image_url": "https://private.example/person.jpg"}
        )

        self.assertEqual(result["visual_mode"], "model_included")
        self.assertEqual(result["influencer_reference_count"], 1)

    def test_runtime_environment_is_never_used_to_infer_provenance(self):
        with patch.dict(
            os.environ,
            {"INFLUENCER_REFERENCE_URLS": "https://private.example/env-person.jpg"},
        ):
            result = self.get_job_for_payload({})

        self.assertEqual(result["visual_mode"], "product_only")
        self.assertEqual(result["influencer_reference_count"], 0)

    def test_missing_private_payload_preserves_legacy_response_shape(self):
        result = self.get_job_for_payload(None)

        self.assertNotIn("visual_mode", result)
        self.assertNotIn("influencer_reference_count", result)

    def test_detail_exposes_only_allowlisted_studio_options(self):
        result = self.get_job_for_payload(
            {
                "visual_mode": "generated_model",
                "channel": "Instagram Reels",
                "cta": "지금 확인",
                "advertising_purpose": "인지도 확보",
                "must_include": "제품 사용 장면",
                "must_exclude": "가격 과장",
                "extra_details": "밝은 주방",
                "prompt": "private prompt",
                "secret": "must-not-leak",
                "influencer_image_urls": ["https://private.example/person.jpg"],
            }
        )

        self.assertEqual(
            result["options"],
            {
                "visual_mode": "generated_model",
                "channel": "Instagram Reels",
                "cta": "지금 확인",
                "advertising_purpose": "인지도 확보",
                "must_include": "제품 사용 장면",
                "must_exclude": "가격 과장",
                "extra_details": "밝은 주방",
            },
        )
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private prompt", encoded)
        self.assertNotIn("must-not-leak", encoded)
        self.assertNotIn("private.example", encoded)

    def test_detail_sanitizes_raw_job_and_candidate_failure_evidence(self):
        row = job_row(None)
        row.status = "FAILED"
        row.stage = "FAILED"
        row.error_message = (
            "provider timeout polling_url=https://provider.example/private "
            "path=/Users/person/runtime/source.mp4"
        )
        row.candidate_count = 1
        row.candidates_json = json.dumps(
            [
                {
                    "candidate_id": "candidate-01",
                    "status": "FAILED",
                    "stage": "VIDEO_GENERATION",
                    "error": row.error_message,
                    "provider_polling_url": "https://provider.example/private-poll",
                    "error_code": "VIDEO_PROVIDER_TIMEOUT",
                    "retryable": True,
                }
            ]
        )

        with patch("app.generation_jobs.SessionLocal", return_value=FakeSession(row)):
            result = get_job("job-1")

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("provider.example", encoded)
        self.assertNotIn("/Users/person", encoded)
        self.assertNotIn("provider_polling_url", encoded)
        self.assertEqual(result["error_code"], "VIDEO_PROVIDER_TIMEOUT")
        self.assertFalse(result["retryable"])
        self.assertEqual(
            result["candidates"][0]["error"],
            "영상 생성 상태 확인이 지연되고 있습니다. 운영자 확인이 필요합니다.",
        )
        self.assertFalse(result["candidates"][0]["retryable"])
        self.assertIn("provider.example", row.error_message)
        self.assertIn(
            "/Users/person",
            json.loads(row.candidates_json)[0]["error"],
        )

    def test_completed_legacy_output_becomes_one_playable_candidate(self):
        row = job_row(None)
        row.status = "COMPLETED"
        row.stage = "COMPLETED"
        row.output_path = "runtime/final/legacy.mp4"
        row.video_job_id = "provider-legacy"
        row.caption_job_id = "caption-legacy"
        row.cost = 1.5

        with patch("app.generation_jobs.SessionLocal", return_value=FakeSession(row)):
            stored = get_job("job-1")

        self.assertEqual(stored["candidate_count"], 1)
        self.assertEqual(stored["completed_candidates"], 1)
        self.assertEqual(stored["candidates"][0]["candidate_id"], "legacy-primary")
        with patch("app.api.v1.final_generation.get_job", return_value=stored):
            result = get_generation_status("job-1")
        self.assertIn("legacy-primary/file", result["candidates"][0]["video_url"])
        self.assertNotIn("output_path", result["candidates"][0])

    def test_final_status_response_preserves_safe_provenance(self):
        with patch(
            "app.api.v1.final_generation.get_job",
            return_value={
                "job_id": "job-1",
                "status": "PROCESSING",
                "visual_mode": "model_included",
                "influencer_reference_count": 2,
                "candidates": [],
            },
        ):
            result = get_generation_status("job-1")

        self.assertEqual(result["visual_mode"], "model_included")
        self.assertEqual(result["influencer_reference_count"], 2)
        self.assertNotIn("payload_json", result)

    def test_privacy_sensitive_image_error_is_stably_non_retryable(self):
        for stage in ("VIDEO_GENERATION", "FAILED"):
            with self.subTest(stage=stage):
                result = _error_metadata(
                    "FAILED",
                    stage,
                    "InputImageSensitiveContentDetected.PrivacyInformation",
                )

                self.assertEqual(result, ("VIDEO_INPUT_INVALID", False))


if __name__ == "__main__":
    unittest.main()
