import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.final_generation import (
    BACKGROUND_VIDEO_MAX_POLL_ATTEMPTS,
    BACKGROUND_VIDEO_MAX_WAIT_SECONDS,
    FinalGenerationBody,
    _generate_narration_with_script_regeneration,
    _generate_video,
    router,
)
from app.tts_generator import SceneAudioDurationError
from app.settings_service import VideoModelCapabilities


class FinalGenerationApiTests(unittest.TestCase):
    def test_background_video_wait_allows_late_completion(self):
        self.assertEqual(BACKGROUND_VIDEO_MAX_WAIT_SECONDS, 18 * 60)
        self.assertEqual(BACKGROUND_VIDEO_MAX_POLL_ATTEMPTS, 216)

    @patch("app.api.v1.final_generation.VideoValidationPipeline")
    @patch("app.api.v1.final_generation.select_video_resolution", return_value="720p")
    @patch("app.api.v1.final_generation.build_video_client")
    @patch("app.api.v1.final_generation.get_video_model_capabilities")
    def test_background_generation_passes_extended_poll_limit(
        self,
        get_capabilities,
        build_client,
        _select_resolution,
        pipeline_class,
    ):
        get_capabilities.return_value = VideoModelCapabilities(
            model_id="video-model",
            name="Video",
            supported_durations=(15,),
            supported_aspect_ratios=("9:16",),
            supported_resolutions=("720p",),
            generate_audio=False,
        )
        pipeline_class.return_value.run.return_value = Mock()

        _generate_video(
            script={"scenes": [{"time_range_sec": {"start": 0, "end": 15}}]},
            image_url="https://example.com/product.jpg",
            influencer_image_url="https://example.com/influencer.jpg",
            detail_image_urls=(),
            service=None,
        )

        build_client.assert_called_once_with(
            None,
            get_capabilities.return_value,
            max_poll_attempts=216,
            on_submitted=None,
        )

    def test_accepts_product_and_existing_script(self):
        body = FinalGenerationBody(
            product={
                "product": {"name": "상품", "image_url": "https://example.com/product.jpg"}
            },
            script={"scenes": []},
            influencer_image_url="https://example.com/influencer.jpg",
        )

        self.assertIsNotNone(body.product)
        self.assertIsNotNone(body.script)

    def test_requires_influencer_image(self):
        with self.assertRaises(ValidationError):
            FinalGenerationBody(
                product={"name": "상품"},
                script={"meta": {}, "summary": {}, "scenes": []},
                image_url="https://example.com/product.jpg",
            )

    def test_accepts_influencer_image(self):
        body = FinalGenerationBody(
            product={"name": "상품"},
            script={"meta": {}, "summary": {}, "scenes": []},
            image_url="https://example.com/product.jpg",
            influencer_image_url="https://example.com/influencer.jpg",
        )

        self.assertEqual(body.influencer_image_url, "https://example.com/influencer.jpg")

    def test_rejects_missing_input(self):
        with self.assertRaises(ValidationError):
            FinalGenerationBody()

    def test_rejects_product_without_existing_script(self):
        with self.assertRaises(ValidationError):
            FinalGenerationBody(
                product={"image_url": "https://example.com/product.jpg"},
                influencer_image_url="https://example.com/influencer.jpg",
            )

    def test_rejects_script_without_product(self):
        with self.assertRaises(ValidationError):
            FinalGenerationBody(
                script={"scenes": []},
                image_url="https://example.com/product.jpg",
                influencer_image_url="https://example.com/influencer.jpg",
            )

    @patch("app.api.v1.final_generation._generate_script")
    @patch("app.api.v1.final_generation.OpenRouterTTSClient")
    def test_regenerates_script_when_scene_tts_exceeds_time_range(
        self, tts_client_class, generate_script
    ):
        initial_script = {"scenes": [{"time_range_sec": {"start": 0, "end": 2}}]}
        regenerated_script = {"scenes": [{"time_range_sec": {"start": 0, "end": 2}}]}
        duration_error = SceneAudioDurationError(1, 2.0, 2.45)
        tts_client_class.return_value.generate_narration.side_effect = [
            duration_error,
            b"valid-audio",
        ]
        generate_script.return_value = regenerated_script

        script, audio = _generate_narration_with_script_regeneration(
            payload={"product": {"name": "상품"}},
            script=initial_script,
            service=None,
        )

        self.assertEqual(script, regenerated_script)
        self.assertEqual(audio, b"valid-audio")
        generate_script.assert_called_once()
        self.assertIs(generate_script.call_args.kwargs["retry_error"], duration_error)
        self.assertFalse(tts_client_class.call_args.kwargs["retry_duration_errors"])

    @patch("app.api.v1.final_generation.validate_product_image_inputs")
    @patch("app.api.v1.final_generation.create_job")
    def test_rejects_invalid_image_before_creating_generation_job(
        self, create_job, validate_images
    ):
        validate_images.side_effect = ValueError(
            "상품 상세 이미지 1번째가 너무 작습니다: 860x1"
        )
        app = FastAPI()
        app.include_router(router)

        with TestClient(app) as client:
            response = client.post(
                "/generate",
                json={
                    "product": {"name": "상품"},
                    "script": {"scenes": []},
                    "image_url": "https://example.com/product.jpg",
                    "influencer_image_url": "https://example.com/influencer.jpg",
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("860x1", response.json()["detail"])
        create_job.assert_not_called()

    @patch("app.api.v1.final_generation.run_generation_job")
    @patch("app.api.v1.final_generation.create_job")
    @patch("app.api.v1.final_generation.validate_product_image_inputs")
    def test_start_returns_job_id_and_status_url(
        self, _validate_images, create_job, run_job
    ):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.post(
                "/generate",
                json={
                    "product": {"name": "상품"},
                    "script": {"scenes": []},
                    "image_url": "https://example.com/product.jpg",
                    "influencer_image_url": "https://example.com/influencer.jpg",
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

    @patch(
        "app.api.v1.final_generation.get_job",
        return_value={"job_id": "job-1", "status": "PROCESSING"},
    )
    def test_status_keeps_processing_without_download_urls(self, _get_job):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.get("/generate/job-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"job_id": "job-1", "status": "PROCESSING"})

    @patch(
        "app.api.v1.final_generation.get_job",
        return_value={
            "job_id": "job-1",
            "status": "FAILED",
            "error": "영상 생성 시간이 초과되었습니다.",
        },
    )
    def test_status_returns_failure_reason(self, _get_job):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.get("/generate/job-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "FAILED")
        self.assertIn("초과", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
