import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, GenerationJobRow
from app.generation_jobs import list_generation_jobs


class GenerationListingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "jobs.db"
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
        self.session_patch = patch(
            "app.generation_jobs.SessionLocal",
            self.session_local,
        )
        self.session_patch.start()

    def tearDown(self):
        self.session_patch.stop()
        self.engine.dispose()
        self.directory.cleanup()

    def add_job(self, job_id, created_at, *, status="COMPLETED"):
        candidates = [
            {
                "candidate_id": "candidate-01",
                "status": "COMPLETED",
                "provider_polling_url": "https://provider.example/private-poll",
                "output_path": "/private/runtime/final.mp4",
                "validation": {
                    "duration_seconds": 15.0,
                    "technical_score": 100,
                },
            }
        ]
        with self.session_local() as session:
            session.add(
                GenerationJobRow(
                    job_id=job_id,
                    status=status,
                    stage=status,
                    input_type="product_and_script",
                    product_json=json.dumps(
                        {
                            "product": {
                                "product_id": f"product-{job_id}",
                                "name": "사과주스 30포",
                            }
                        }
                    ),
                    script_json=json.dumps(
                        {
                            "scenes": [
                                {"time_range_sec": {"start": 0, "end": 15}}
                            ]
                        }
                    ),
                    payload_json=json.dumps(
                        {
                            "template": {
                                "id": "ugc_full_15",
                                "version": 1,
                                "duration_seconds": 15,
                            },
                            "visual_mode": "generated_model",
                            "secret": "must-not-leak",
                        }
                    ),
                    image_url="https://cdn.example/product.jpg",
                    candidate_count=1,
                    candidates_json=json.dumps(candidates),
                    output_path="/private/runtime/final.mp4",
                    cost=5.7,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            session.commit()

    def test_returns_safe_summary_without_payload_or_private_paths(self):
        now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        self.add_job("job-1", now)

        result = list_generation_jobs()
        encoded = json.dumps(result, ensure_ascii=False)

        self.assertEqual(result["items"][0]["product"]["name"], "사과주스 30포")
        self.assertEqual(result["items"][0]["template"]["id"], "ugc_full_15")
        self.assertEqual(result["items"][0]["primary_candidate"]["technical_score"], 100)
        self.assertFalse(result["items"][0]["asset_fidelity"]["package_text_verified"])
        self.assertNotIn("must-not-leak", encoded)
        self.assertNotIn("private-poll", encoded)
        self.assertNotIn("/private/", encoded)
        self.assertNotIn("payload_json", encoded)

    def test_cursor_pages_are_stable_and_status_can_be_filtered(self):
        now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        self.add_job("job-older", now - timedelta(seconds=1))
        self.add_job("job-newer", now)
        self.add_job("job-failed", now + timedelta(seconds=1), status="FAILED")

        first = list_generation_jobs(limit=1, status="COMPLETED")
        second = list_generation_jobs(
            limit=1,
            status="COMPLETED",
            cursor=first["next_cursor"],
        )

        self.assertEqual(first["items"][0]["job_id"], "job-newer")
        self.assertEqual(second["items"][0]["job_id"], "job-older")
        self.assertIsNone(second["next_cursor"])

    def test_legacy_job_without_template_remains_listable(self):
        now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        with self.session_local() as session:
            session.add(
                GenerationJobRow(
                    job_id="legacy",
                    status="FAILED",
                    stage="VIDEO_GENERATION",
                    input_type="product_and_script",
                    product_json=json.dumps({"name": "기존 상품"}),
                    script_json=None,
                    payload_json=None,
                    image_url=None,
                    candidate_count=0,
                    candidates_json="[]",
                    error_message="기존 오류",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        item = list_generation_jobs()["items"][0]

        self.assertEqual(item["job_id"], "legacy")
        self.assertIsNone(item["template"])
        self.assertEqual(item["error"]["message"], "기존 오류")

    def test_rejects_invalid_cursor(self):
        with self.assertRaisesRegex(ValueError, "cursor"):
            list_generation_jobs(cursor="not-valid-base64***")


if __name__ == "__main__":
    unittest.main()
