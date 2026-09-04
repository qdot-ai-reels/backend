import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.final_generation import router
from app.db import Base, GenerationJobRow


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
        self.app = FastAPI()
        self.app.include_router(router)

    def tearDown(self):
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
                "job_id": "job-safe-1",
                "status": "PROCESSING",
                "stage": "VIDEO_GENERATION",
                "status_url": "/api/v1/reels/generate/job-safe-1",
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


if __name__ == "__main__":
    unittest.main()
