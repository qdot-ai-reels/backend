import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.api.v1.products import router
from app.db import (
    Base,
    GenerationJobRow,
    GenerationRequestRow,
    ProductActivationAuditRow,
    ProductCatalogRow,
)
from app.products import (
    BUILTIN_PRODUCT_ID,
    ProductAssetValidationError,
    ProductCatalogInactiveError,
    ProductCatalogRevisionConflictError,
    resolve_active_generation_product,
    seed_builtin_product_catalog,
)
from app.generation_jobs import (
    GENERATION_REQUEST_IN_PROGRESS,
    GenerationRequestReservation,
    PaidRetryAuthorizationError,
    create_job,
    reserve_candidate_retry,
)


class ProductCatalogApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "products.db"
        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.session_local = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
        )
        self.session_patch = patch("app.products.SessionLocal", self.session_local)
        self.asset_patch = patch("app.products._verify_assets")
        self.session_patch.start()
        self.verify_assets = self.asset_patch.start()
        self.addCleanup(self.asset_patch.stop)
        self.addCleanup(self.session_patch.stop)
        self.app = FastAPI()
        self.app.include_router(router)

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    @staticmethod
    def product_body(**changes):
        body = {
            "product_id": "catalog-product-1",
            "event_id": "event-1",
            "event_name": "가을 공구",
            "curator": "큐레이터",
            "name": "유기농 사과주스",
            "option": "30포",
            "sale_price": 22000,
            "discount_label": "60% 할인",
            "image_url": "https://cdn.example.com/product.jpg",
            "detail_image_urls": ["https://cdn.example.com/detail.webp"],
            "square_output_strategy": "center_crop",
            "raw_product": {"selling_point": "사과 100%"},
            "is_active": False,
        }
        body.update(changes)
        return body

    def test_create_is_inactive_and_hidden_until_reviewed_activation(self):
        with TestClient(self.app) as client:
            created_response = client.post("/products", json=self.product_body())
            active_list = client.get("/products")
            full_list = client.get("/products?include_inactive=true")

            created = created_response.json()
            rejected_activation = client.post(
                "/products/catalog-product-1/activate",
                json={
                    "expected_revision": created["revision"],
                    "asset_review_acknowledged": False,
                    "review_note": "상품과 이미지 확인",
                },
            )
            activated_response = client.post(
                "/products/catalog-product-1/activate",
                json={
                    "expected_revision": created["revision"],
                    "asset_review_acknowledged": True,
                    "review_note": "대표 상품과 이미지 의미 일치 확인",
                },
            )

        self.assertEqual(created_response.status_code, 201)
        self.assertEqual(created_response.headers["cache-control"], "no-store")
        self.assertFalse(created["is_active"])
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["raw_product"]["product_id"], "catalog-product-1")
        self.assertEqual(created["raw_product"]["catalog_revision"], 1)
        self.assertEqual(active_list.json()["items"], [])
        self.assertEqual(full_list.json()["total"], 1)
        self.assertEqual(rejected_activation.status_code, 422)
        activated = activated_response.json()
        self.assertEqual(activated_response.status_code, 200)
        self.assertTrue(activated["is_active"])
        self.assertEqual(activated["revision"], 2)
        self.assertEqual(
            activated["activation_review_note"],
            "대표 상품과 이미지 의미 일치 확인",
        )
        with self.session_local() as session:
            self.assertEqual(
                session.scalar(select(func.count(ProductActivationAuditRow.id))),
                1,
            )

    def test_create_cannot_bypass_review_with_is_active_true(self):
        with TestClient(self.app) as client:
            response = client.post(
                "/products",
                json=self.product_body(is_active=True),
            )

        self.assertEqual(response.status_code, 422)
        with self.session_local() as session:
            self.assertIsNone(session.get(ProductCatalogRow, "catalog-product-1"))

    def test_duplicate_product_id_fails_before_remote_asset_probe(self):
        with TestClient(self.app) as client:
            created = client.post("/products", json=self.product_body())
            self.verify_assets.reset_mock()
            duplicate = client.post("/products", json=self.product_body())

        self.assertEqual(created.status_code, 201)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            duplicate.json()["detail"]["code"], "PRODUCT_ALREADY_EXISTS"
        )
        self.verify_assets.assert_not_called()

    def test_asset_validation_error_has_stable_code_and_does_not_persist(self):
        self.verify_assets.side_effect = ProductAssetValidationError(
            "상품 이미지를 Production 입력으로 사용할 수 없습니다."
        )
        with TestClient(self.app) as client:
            response = client.post("/products", json=self.product_body())

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "PRODUCT_ASSET_INVALID")
        with self.session_local() as session:
            self.assertIsNone(session.get(ProductCatalogRow, "catalog-product-1"))

    def test_update_requires_current_revision_and_invalidates_activation(self):
        with TestClient(self.app) as client:
            created = client.post("/products", json=self.product_body()).json()
            activated = client.post(
                "/products/catalog-product-1/activate",
                json={
                    "expected_revision": created["revision"],
                    "asset_review_acknowledged": True,
                    "review_note": "의미 일치 확인",
                },
            ).json()
            stale = client.put(
                "/products/catalog-product-1",
                json={"expected_revision": 1, "name": "오래된 수정"},
            )
            self.verify_assets.reset_mock()
            updated_response = client.put(
                "/products/catalog-product-1",
                json={
                    "expected_revision": activated["revision"],
                    "name": "유기농 사과주스 리뉴얼",
                    "raw_product": {
                        **activated["raw_product"],
                        "selling_point": "새 설명",
                    },
                },
            )

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "PRODUCT_REVISION_CONFLICT")
        updated = updated_response.json()
        self.assertEqual(updated["revision"], 3)
        self.assertFalse(updated["is_active"])
        self.assertIsNone(updated["activated_at"])
        self.assertIsNone(updated["activation_review_note"])
        self.assertEqual(updated["raw_product"]["catalog_revision"], 3)
        self.verify_assets.assert_not_called()

    def test_archive_is_idempotent_and_activation_recovers_it(self):
        with TestClient(self.app) as client:
            created = client.post("/products", json=self.product_body()).json()
            activated = client.post(
                "/products/catalog-product-1/activate",
                json={
                    "expected_revision": created["revision"],
                    "asset_review_acknowledged": True,
                    "review_note": "의미 일치 확인",
                },
            ).json()
            first_archive = client.delete(
                "/products/catalog-product-1",
                params={"expected_revision": activated["revision"]},
            )
            repeated_archive = client.delete(
                "/products/catalog-product-1",
                params={"expected_revision": activated["revision"]},
            )
            restored = client.post(
                "/products/catalog-product-1/activate",
                json={
                    "expected_revision": first_archive.json()["revision"],
                    "asset_review_acknowledged": True,
                    "review_note": "보관 복구 전 재검수",
                },
            )

        self.assertEqual(first_archive.status_code, 200)
        self.assertIsNotNone(first_archive.json()["archived_at"])
        self.assertEqual(repeated_archive.status_code, 200)
        self.assertEqual(
            repeated_archive.json()["revision"], first_archive.json()["revision"]
        )
        self.assertTrue(restored.json()["is_active"])
        self.assertIsNone(restored.json()["archived_at"])

    def test_seed_is_idempotent_and_never_overwrites_operator_state(self):
        seed_builtin_product_catalog(bind=self.engine)
        with self.session_local() as session:
            row = session.get(ProductCatalogRow, BUILTIN_PRODUCT_ID)
            row.name = "운영자 수정 이름"
            row.is_active = False
            row.revision = 9
            session.commit()

        seed_builtin_product_catalog(bind=self.engine)

        with self.session_local() as session:
            row = session.get(ProductCatalogRow, BUILTIN_PRODUCT_ID)
            self.assertEqual(row.name, "운영자 수정 이름")
            self.assertFalse(row.is_active)
            self.assertEqual(row.revision, 9)
            self.assertEqual(
                session.scalar(select(func.count(ProductCatalogRow.product_id))),
                1,
            )
            self.assertEqual(
                session.scalar(select(func.count(ProductActivationAuditRow.id))),
                1,
            )

    def test_generation_resolution_is_active_revision_bound_and_canonical(self):
        with TestClient(self.app) as client:
            created = client.post("/products", json=self.product_body()).json()

        with self.assertRaises(ProductCatalogInactiveError):
            resolve_active_generation_product(
                created["raw_product"], created["image_url"], created["revision"]
            )

        with TestClient(self.app) as client:
            active = client.post(
                "/products/catalog-product-1/activate",
                json={
                    "expected_revision": created["revision"],
                    "asset_review_acknowledged": True,
                    "review_note": "의미 일치 확인",
                },
            ).json()

        with self.assertRaises(ProductCatalogRevisionConflictError):
            resolve_active_generation_product(
                active["raw_product"], active["image_url"], 1
            )
        with self.assertRaises(ProductCatalogRevisionConflictError):
            resolve_active_generation_product(
                active["raw_product"], "https://evil.example/tampered.jpg", 2
            )

        tampered = {**active["raw_product"], "name": "조작된 이름"}
        resolved = resolve_active_generation_product(
            tampered,
            active["image_url"],
            active["revision"],
        )
        self.assertEqual(resolved["product"]["name"], "유기농 사과주스")
        self.assertEqual(resolved["revision"], active["revision"])

    def test_job_acceptance_rechecks_inactive_product_in_same_transaction(self):
        with TestClient(self.app) as client:
            created = client.post("/products", json=self.product_body()).json()
            active = client.post(
                "/products/catalog-product-1/activate",
                json={
                    "expected_revision": created["revision"],
                    "asset_review_acknowledged": True,
                    "review_note": "생성 승인 전 검수",
                },
            ).json()
        with self.session_local() as session:
            session.add(
                GenerationRequestRow(
                    client_request_id="catalog-cas-inactive",
                    request_hash="request-hash",
                    state=GENERATION_REQUEST_IN_PROGRESS,
                    owner_token="owner-token",
                )
            )
            session.commit()

        # Simulate the operator deactivating the product after the route's
        # first read but before durable ACCEPTED/job creation.
        with TestClient(self.app) as client:
            deactivated = client.post(
                "/products/catalog-product-1/deactivate",
                json={"expected_revision": active["revision"]},
            )
        self.assertEqual(deactivated.status_code, 200)

        with (
            patch("app.generation_jobs.SessionLocal", self.session_local),
            self.assertRaises(ProductCatalogInactiveError),
        ):
            create_job(
                "catalog-cas-inactive-job",
                input_type="product_template",
                product=active["raw_product"],
                script=None,
                image_url=active["image_url"],
                candidate_count=1,
                client_request_id="catalog-cas-inactive",
                request_hash="request-hash",
                reservation_owner_token="owner-token",
                catalog_product_id="catalog-product-1",
                catalog_revision=active["revision"],
            )

        with self.session_local() as session:
            request = session.get(GenerationRequestRow, "catalog-cas-inactive")
            self.assertEqual(request.state, GENERATION_REQUEST_IN_PROGRESS)
            self.assertEqual(request.owner_token, "owner-token")
            self.assertIsNone(
                session.get(GenerationJobRow, "catalog-cas-inactive-job")
            )

    def test_job_acceptance_rechecks_catalog_revision_in_same_transaction(self):
        with TestClient(self.app) as client:
            created = client.post("/products", json=self.product_body()).json()
            active = client.post(
                "/products/catalog-product-1/activate",
                json={
                    "expected_revision": created["revision"],
                    "asset_review_acknowledged": True,
                    "review_note": "생성 승인 전 검수",
                },
            ).json()
        with self.session_local() as session:
            row = session.get(ProductCatalogRow, "catalog-product-1")
            row.revision += 1
            session.add(
                GenerationRequestRow(
                    client_request_id="catalog-cas-revision",
                    request_hash="request-hash",
                    state=GENERATION_REQUEST_IN_PROGRESS,
                    owner_token="owner-token",
                )
            )
            session.commit()

        with (
            patch("app.generation_jobs.SessionLocal", self.session_local),
            self.assertRaises(ProductCatalogRevisionConflictError),
        ):
            create_job(
                "catalog-cas-revision-job",
                input_type="product_template",
                product=active["raw_product"],
                script=None,
                image_url=active["image_url"],
                candidate_count=1,
                client_request_id="catalog-cas-revision",
                request_hash="request-hash",
                reservation_owner_token="owner-token",
                catalog_product_id="catalog-product-1",
                catalog_revision=active["revision"],
            )

        with self.session_local() as session:
            request = session.get(GenerationRequestRow, "catalog-cas-revision")
            self.assertEqual(request.state, GENERATION_REQUEST_IN_PROGRESS)
            self.assertIsNone(
                session.get(GenerationJobRow, "catalog-cas-revision-job")
            )

    def test_template_retry_reservation_consumes_quote_allowance_once(self):
        with TestClient(self.app) as client:
            created = client.post("/products", json=self.product_body()).json()
            active = client.post(
                "/products/catalog-product-1/activate",
                json={
                    "expected_revision": created["revision"],
                    "asset_review_acknowledged": True,
                    "review_note": "재시도 승인 전 검수",
                },
            ).json()
        candidates = [
            {
                "candidate_id": "candidate-01",
                "status": "FAILED",
                "stage": "VIDEO_GENERATION",
                "retryable": True,
                "attempts": 2,
                "cost": 1.25,
                "paid_retry_count": 0,
            }
        ]
        with self.session_local() as session:
            session.add(
                GenerationJobRow(
                    job_id="quoted-retry-job",
                    status="FAILED",
                    stage="FAILED",
                    input_type="product_template",
                    candidate_count=1,
                    candidates_json=json.dumps(candidates),
                )
            )
            session.commit()

        with patch("app.generation_jobs.SessionLocal", self.session_local):
            prior = reserve_candidate_retry(
                "quoted-retry-job",
                "candidate-01",
                paid_retry_limit=1,
                catalog_product_id="catalog-product-1",
                catalog_revision=active["revision"],
            )
        self.assertEqual(prior["cost"], 1.25)
        with self.session_local() as session:
            row = session.get(GenerationJobRow, "quoted-retry-job")
            reserved = json.loads(row.candidates_json)[0]
            self.assertEqual(row.status, "PROCESSING")
            self.assertEqual(reserved["status"], "PENDING")
            self.assertEqual(reserved["paid_retry_count"], 1)
            row.status = "FAILED"
            reserved["status"] = "FAILED"
            reserved["retryable"] = True
            row.candidates_json = json.dumps([reserved])
            session.commit()

        with (
            patch("app.generation_jobs.SessionLocal", self.session_local),
            self.assertRaises(PaidRetryAuthorizationError),
        ):
            reserve_candidate_retry(
                "quoted-retry-job",
                "candidate-01",
                paid_retry_limit=1,
                catalog_product_id="catalog-product-1",
                catalog_revision=active["revision"],
            )


class ProductGenerationAuthorityApiTests(unittest.TestCase):
    @staticmethod
    def app():
        from app.api.v1.final_generation import router as generation_router

        app = FastAPI()
        app.include_router(generation_router)
        return app

    @staticmethod
    def body():
        return {
            "product": {
                "product_id": "catalog-product-1",
                "catalog_revision": 3,
                "name": "상품",
            },
            "image_url": "https://cdn.example.com/product.jpg",
            "template_id": "ugc_quick_4",
            "product_catalog_revision": 3,
            "quote_id": "quote-1",
            "visual_mode": "generated_model",
            "candidate_count": 1,
            "client_request_id": "catalog-authority-request",
        }

    @staticmethod
    def reservation():
        return GenerationRequestReservation(
            client_request_id="catalog-authority-request",
            request_hash="request-hash",
            state=GENERATION_REQUEST_IN_PROGRESS,
            is_owner=True,
            owner_token="owner-token",
        )

    def test_inactive_catalog_product_is_rejected_before_quote_or_paid_work(self):
        with (
            patch(
                "app.api.v1.final_generation.reserve_generation_request",
                return_value=self.reservation(),
            ),
            patch(
                "app.api.v1.final_generation.reject_generation_request",
                return_value=True,
            ) as reject_request,
            patch(
                "app.api.v1.final_generation.resolve_active_generation_product",
                side_effect=ProductCatalogInactiveError("비활성 상품"),
            ),
            patch(
                "app.api.v1.final_generation.validate_generation_quote"
            ) as validate_quote,
            patch("app.api.v1.final_generation.create_job") as create_job,
            TestClient(self.app()) as client,
        ):
            response = client.post("/generate", json=self.body())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "PRODUCT_UNAVAILABLE")
        self.assertEqual(reject_request.call_args.kwargs["code"], "PRODUCT_UNAVAILABLE")
        validate_quote.assert_not_called()
        create_job.assert_not_called()

    def test_stale_catalog_revision_has_refreshable_conflict_code(self):
        with (
            patch(
                "app.api.v1.final_generation.reserve_generation_request",
                return_value=self.reservation(),
            ),
            patch(
                "app.api.v1.final_generation.reject_generation_request",
                return_value=True,
            ),
            patch(
                "app.api.v1.final_generation.resolve_active_generation_product",
                side_effect=ProductCatalogRevisionConflictError("상품 변경됨"),
            ),
            patch(
                "app.api.v1.final_generation.validate_generation_quote"
            ) as validate_quote,
            patch("app.api.v1.final_generation.create_job") as create_job,
            TestClient(self.app()) as client,
        ):
            response = client.post("/generate", json=self.body())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "PRODUCT_CATALOG_CHANGED"
        )
        validate_quote.assert_not_called()
        create_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
