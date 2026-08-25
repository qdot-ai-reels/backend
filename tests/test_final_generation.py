import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.final_generation import FinalGenerationBody, router


class FinalGenerationApiTests(unittest.TestCase):
    def test_accepts_original_product_json(self):
        body = FinalGenerationBody(
            product={
                "product": {"name": "상품", "image_url": "https://example.com/product.jpg"}
            }
        )

        self.assertIsNotNone(body.product)
        self.assertIsNone(body.script)

    def test_accepts_generated_script_json_with_separate_image(self):
        body = FinalGenerationBody(
            script={"meta": {}, "summary": {}, "scenes": []},
            image_url="https://example.com/product.jpg",
        )

        self.assertIsNotNone(body.script)
        self.assertIsNone(body.product)

    def test_rejects_missing_input(self):
        with self.assertRaises(ValidationError):
            FinalGenerationBody()

    def test_rejects_both_input_modes(self):
        with self.assertRaises(ValidationError):
            FinalGenerationBody(
                product={"image_url": "https://example.com/product.jpg"},
                script={"scenes": []},
            )

    @patch("app.api.v1.final_generation.run_generation_job")
    @patch("app.api.v1.final_generation.create_job")
    def test_start_returns_job_id_and_status_url(self, create_job, run_job):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.post(
                "/generate",
                json={
                    "script": {"scenes": []},
                    "image_url": "https://example.com/product.jpg",
                },
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "PENDING")
        self.assertIn(payload["job_id"], payload["status_url"])
        create_job.assert_called_once()
        run_job.assert_called_once()

    @patch(
        "app.api.v1.final_generation.get_job",
        return_value={
            "job_id": "job-1",
            "status": "COMPLETED",
            "input_type": "script",
            "output_path": "runtime/final/job-1/final.mp4",
        },
    )
    def test_status_returns_final_playback_urls_when_completed(self, _get_job):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.get("/generate/job-1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["video_url"], "/api/v1/reels/generate/job-1/file")
        self.assertIn("download=true", payload["download_url"])


if __name__ == "__main__":
    unittest.main()
