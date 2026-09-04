import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.final_generation import FinalGenerationBody, router
from app.db import Base, GenerationJobRow
from app.generation_jobs import (
    GENERATION_REQUEST_ACCEPTED,
    GENERATION_REQUEST_CONFLICT,
    GENERATION_REQUEST_IN_PROGRESS,
    create_job,
    get_generation_request,
    reserve_generation_request,
)
from app.generation_quotes import canonical_request_hash
from app.prompt_versions import builtin_prompt_snapshot


class GenerationRequestLookupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        database_path = Path(self.directory.name) / "generation-requests.db"
        self.engine = create_engine(
            f"sqlite:///{database_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.session_local = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
        )
        self.session_patch = patch(
            "app.generation_jobs.SessionLocal",
            self.session_local,
        )
        self.session_patch.start()
        self.prompt_snapshot_patch = patch(
            "app.api.v1.final_generation._load_quoted_prompt_snapshot",
            return_value=builtin_prompt_snapshot(),
        )
        self.prompt_snapshot_patch.start()
        self.app = FastAPI()
        self.app.include_router(router)

    @staticmethod
    def valid_request(client_request_id="browser-submit-new"):
        return {
            "product": {"name": "상품"},
            "image_url": "https://example.com/product.jpg",
            "template_id": "ugc_quick_4",
            "quote_id": "quote-1",
            "visual_mode": "generated_model",
            "candidate_count": 1,
            "client_request_id": client_request_id,
        }

    @staticmethod
    def raw_request_hash(request):
        payload = FinalGenerationBody.model_validate(request).model_dump(mode="json")
        payload.pop("client_request_id", None)
        return canonical_request_hash(payload)

    def tearDown(self):
        self.prompt_snapshot_patch.stop()
        self.session_patch.stop()
        self.engine.dispose()
        self.directory.cleanup()

    def test_found_request_returns_only_safe_recovery_fields_without_provider_call(self):
        with self.session_local() as session:
            session.add(
                GenerationJobRow(
                    job_id="job-safe-1",
                    status="PROCESSING",
                    stage="VIDEO_GENERATION",
                    input_type="product_template",
                    payload_json=json.dumps({"secret": "must-not-leak"}),
                    client_request_id="browser-submit-1",
                    request_hash="private-request-hash",
                )
            )
            session.commit()

        with (
            patch("app.api.v1.final_generation.build_video_client") as build_video,
            patch(
                "app.api.v1.final_generation.get_video_model_capabilities"
            ) as get_capabilities,
            TestClient(self.app) as client,
        ):
            response = client.get("/generation-requests/browser-submit-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "client_request_id": "browser-submit-1",
                "request_state": "ACCEPTED",
                "job_id": "job-safe-1",
                "status": "PROCESSING",
                "stage": "VIDEO_GENERATION",
                "status_url": "/api/v1/reels/generate/job-safe-1",
                "error": None,
                "recoverable": False,
                "retry_after_seconds": None,
            },
        )
        self.assertNotIn("must-not-leak", response.text)
        self.assertNotIn("private-request-hash", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        build_video.assert_not_called()
        get_capabilities.assert_not_called()

    def test_missing_request_returns_stable_structured_404(self):
        with TestClient(self.app) as client:
            response = client.get("/generation-requests/not-submitted")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["detail"],
            {
                "code": "GENERATION_REQUEST_NOT_FOUND",
                "message": (
                    "client_request_id에 해당하는 생성 요청을 찾을 수 없습니다."
                ),
            },
        )
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_concurrent_same_body_reservation_has_exactly_one_owner(self):
        barrier = threading.Barrier(2)
        now = datetime.now(timezone.utc)

        def reserve():
            barrier.wait(timeout=2)
            return reserve_generation_request(
                "concurrent-request",
                "same-hash",
                now=now,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: reserve(), range(2)))

        self.assertEqual(sum(result.is_owner for result in results), 1)
        self.assertEqual(
            {result.state for result in results},
            {GENERATION_REQUEST_IN_PROGRESS},
        )
        self.assertEqual(
            sum(result.owner_token is not None for result in results),
            1,
        )

    def test_pre_insert_recovery_returns_in_progress_without_duplicate_work(self):
        request = self.valid_request("pre-insert-request")
        reservation = reserve_generation_request(
            request["client_request_id"],
            self.raw_request_hash(request),
        )
        self.assertTrue(reservation.is_owner)

        with (
            patch(
                "app.api.v1.final_generation.validate_product_image_inputs"
            ) as validate_product,
            patch(
                "app.api.v1.final_generation.validate_generation_quote"
            ) as validate_quote,
            patch("app.api.v1.final_generation.create_job") as create_job_mock,
            patch(
                "app.api.v1.final_generation.InProcessGenerationDispatcher.enqueue"
            ) as enqueue,
            TestClient(self.app) as client,
        ):
            lookup = client.get("/generation-requests/pre-insert-request")
            replay = client.post("/generate", json=request)

        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.json()["request_state"], "IN_PROGRESS")
        self.assertIsNone(lookup.json()["job_id"])
        self.assertFalse(lookup.json()["recoverable"])
        self.assertGreater(lookup.json()["retry_after_seconds"], 0)
        self.assertLessEqual(lookup.json()["retry_after_seconds"], 300)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["request_state"], "IN_PROGRESS")
        self.assertIsNone(replay.json()["job_id"])
        encoded = json.dumps(lookup.json(), ensure_ascii=False)
        self.assertNotIn("owner_token", encoded)
        self.assertNotIn("request_hash", encoded)
        self.assertNotIn("lease_expires_at", encoded)
        validate_product.assert_not_called()
        validate_quote.assert_not_called()
        create_job_mock.assert_not_called()
        enqueue.assert_not_called()

    def test_concurrent_same_id_posts_enqueue_exactly_once(self):
        request = self.valid_request("concurrent-post-request")
        owner_entered_validation = threading.Event()
        release_owner = threading.Event()

        def validate_product(*_args, **_kwargs):
            owner_entered_validation.set()
            if not release_owner.wait(timeout=3):
                raise AssertionError("동시 요청 테스트 owner release timeout")

        def post_request():
            with TestClient(self.app) as client:
                return client.post("/generate", json=request)

        quote = {
            "quote_id": "quote-1",
            "currency": "USD",
            "total": {"min": 1.0, "expected": 1.2, "max": 1.4},
            "coverage": "video_only",
        }
        with (
            patch(
                "app.api.v1.final_generation.validate_product_image_inputs",
                side_effect=validate_product,
            ) as validate_product_mock,
            patch(
                "app.api.v1.final_generation.validate_normalized_influencer_references"
            ),
            patch(
                "app.api.v1.final_generation._selected_video_model_id",
                return_value="video/model",
            ),
            patch(
                "app.api.v1.final_generation.validate_generation_quote",
                return_value=quote,
            ) as validate_quote,
            patch(
                "app.api.v1.final_generation.InProcessGenerationDispatcher.enqueue"
            ) as enqueue,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            owner_future = executor.submit(post_request)
            self.assertTrue(owner_entered_validation.wait(timeout=2))
            follower_future = executor.submit(post_request)
            follower = follower_future.result(timeout=2)
            release_owner.set()
            owner = owner_future.result(timeout=3)

        self.assertEqual(owner.status_code, 202)
        self.assertIsNotNone(owner.json()["job_id"])
        self.assertEqual(follower.status_code, 202)
        self.assertEqual(follower.json()["request_state"], "IN_PROGRESS")
        self.assertIsNone(follower.json()["job_id"])
        self.assertEqual(validate_product_mock.call_count, 1)
        self.assertEqual(validate_quote.call_count, 1)
        self.assertEqual(enqueue.call_count, 1)

    def test_rejection_is_persisted_and_same_body_does_not_revalidate(self):
        request = self.valid_request("rejected-request")
        request.pop("image_url")
        with (
            patch(
                "app.api.v1.final_generation.validate_generation_quote"
            ) as validate_quote,
            patch(
                "app.api.v1.final_generation.InProcessGenerationDispatcher.enqueue"
            ) as enqueue,
            TestClient(self.app) as client,
        ):
            rejected = client.post("/generate", json=request)
            lookup = client.get("/generation-requests/rejected-request")
            repeated = client.post("/generate", json=request)

        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(
            rejected.json()["detail"]["code"],
            "REQUEST_VALIDATION_FAILED",
        )
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.json()["request_state"], "REJECTED")
        self.assertEqual(lookup.json()["status"], "REJECTED")
        self.assertEqual(
            lookup.json()["error"],
            {
                "code": "REQUEST_VALIDATION_FAILED",
                "message": "상품 이미지 URL이 필요합니다.",
                "http_status": 422,
            },
        )
        self.assertEqual(repeated.status_code, 422)
        self.assertEqual(repeated.json()["detail"], rejected.json()["detail"])
        validate_quote.assert_not_called()
        enqueue.assert_not_called()

    def test_accepted_request_replays_without_duplicate_background_work(self):
        request = self.valid_request("accepted-request")
        quote = {
            "quote_id": "quote-1",
            "currency": "USD",
            "total": {"min": 1.0, "expected": 1.2, "max": 1.4},
            "coverage": "video_only",
        }
        with (
            patch("app.api.v1.final_generation.validate_product_image_inputs"),
            patch(
                "app.api.v1.final_generation.validate_normalized_influencer_references"
            ),
            patch(
                "app.api.v1.final_generation._selected_video_model_id",
                return_value="video/model",
            ),
            patch(
                "app.api.v1.final_generation.validate_generation_quote",
                return_value=quote,
            ) as validate_quote,
            patch(
                "app.api.v1.final_generation.InProcessGenerationDispatcher.enqueue"
            ) as enqueue,
            TestClient(self.app) as client,
        ):
            accepted = client.post("/generate", json=request)
            replay = client.post("/generate", json=request)
            conflict = client.post(
                "/generate",
                json={**request, "candidate_count": 2},
            )
            lookup = client.get("/generation-requests/accepted-request")

        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["job_id"], accepted.json()["job_id"])
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.json()["detail"]["code"],
            "IDEMPOTENCY_CONFLICT",
        )
        self.assertEqual(lookup.json()["request_state"], GENERATION_REQUEST_ACCEPTED)
        self.assertEqual(lookup.json()["job_id"], accepted.json()["job_id"])
        self.assertEqual(validate_quote.call_count, 1)
        self.assertEqual(enqueue.call_count, 1)

    def test_different_body_conflicts_before_validation_or_background_work(self):
        first = self.valid_request("different-body-request")
        reserve_generation_request(
            first["client_request_id"],
            self.raw_request_hash(first),
        )
        changed = {**first, "candidate_count": 2}

        with (
            patch(
                "app.api.v1.final_generation.validate_product_image_inputs"
            ) as validate_product,
            patch(
                "app.api.v1.final_generation.validate_generation_quote"
            ) as validate_quote,
            patch(
                "app.api.v1.final_generation.InProcessGenerationDispatcher.enqueue"
            ) as enqueue,
            TestClient(self.app) as client,
        ):
            response = client.post("/generate", json=changed)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"],
            "IDEMPOTENCY_CONFLICT",
        )
        validate_product.assert_not_called()
        validate_quote.assert_not_called()
        enqueue.assert_not_called()

    def test_expired_lease_is_reclaimed_and_stale_owner_is_fenced(self):
        started_at = datetime(2026, 9, 4, tzinfo=timezone.utc)
        original = reserve_generation_request(
            "expired-lease-request",
            "same-hash",
            now=started_at,
        )
        expired_lookup = get_generation_request("expired-lease-request")
        self.assertTrue(expired_lookup["recoverable"])
        self.assertEqual(expired_lookup["retry_after_seconds"], 0)
        reclaimed = reserve_generation_request(
            "expired-lease-request",
            "same-hash",
            now=started_at + timedelta(minutes=5, seconds=1),
        )

        self.assertTrue(original.is_owner)
        self.assertTrue(reclaimed.is_owner)
        self.assertNotEqual(original.owner_token, reclaimed.owner_token)
        stale_created = create_job(
            "stale-job",
            input_type="product_template",
            product={"name": "상품"},
            script=None,
            image_url="https://example.com/product.jpg",
            candidate_count=1,
            client_request_id="expired-lease-request",
            request_hash="same-hash",
            reservation_owner_token=original.owner_token,
        )
        self.assertFalse(stale_created)
        accepted = create_job(
            "reclaimed-job",
            input_type="product_template",
            product={"name": "상품"},
            script=None,
            image_url="https://example.com/product.jpg",
            candidate_count=1,
            client_request_id="expired-lease-request",
            request_hash="same-hash",
            reservation_owner_token=reclaimed.owner_token,
        )
        self.assertTrue(accepted)
        lookup = get_generation_request("expired-lease-request")
        self.assertEqual(lookup["request_state"], GENERATION_REQUEST_ACCEPTED)
        self.assertEqual(lookup["job_id"], "reclaimed-job")

    def test_direct_different_hash_reservation_is_conflict(self):
        reserve_generation_request("hash-conflict", "first-hash")
        conflict = reserve_generation_request("hash-conflict", "second-hash")

        self.assertEqual(conflict.state, GENERATION_REQUEST_CONFLICT)
        self.assertFalse(conflict.is_owner)


if __name__ == "__main__":
    unittest.main()
