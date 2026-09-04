import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.final_generation import (
    BACKGROUND_VIDEO_MAX_POLL_ATTEMPTS,
    BACKGROUND_VIDEO_MAX_WAIT_SECONDS,
    FinalGenerationBody,
    _generate_narration_with_script_regeneration,
    _generate_narration_with_deterministic_fallback,
    _generate_script,
    _generate_video,
    _fail_unresolved_candidates,
    _run_candidate,
    _script_duration_seconds,
    get_generation_status,
    resolve_influencer_image_urls,
    retry_candidate,
    router,
)
from fastapi import BackgroundTasks
from app.generation_jobs import _error_metadata, _status_message
from app.tts_generator import SceneAudioDurationError
from app.script_generator import ScriptDialogueLengthError
from app.settings_service import VideoModelCapabilities
from app.video_validation_pipeline import PipelineResult, PipelineStatus
from app.video_validator import ValidationResult, VideoMetadata


class FinalGenerationApiTests(unittest.TestCase):
    def test_candidate_count_defaults_to_three_and_is_bounded(self):
        body = FinalGenerationBody(
            product={"name": "상품"},
            script={"scenes": []},
            image_url="https://example.com/product.jpg",
        )
        self.assertEqual(body.candidate_count, 3)
        self.assertIsNone(body.visual_mode)
        self.assertEqual(body.square_output_strategy, "reject")
        opted_in = FinalGenerationBody(
            product={"name": "상품"},
            script={"scenes": []},
            image_url="https://example.com/product.jpg",
            square_output_strategy="center_crop",
        )
        self.assertEqual(opted_in.square_output_strategy, "center_crop")
        for invalid in (0, 5):
            with self.assertRaises(ValidationError):
                FinalGenerationBody(
                    product={"name": "상품"},
                    script={"scenes": []},
                    image_url="https://example.com/product.jpg",
                    candidate_count=invalid,
                )

    def test_explicit_product_only_never_uses_server_default_influencer(self):
        with patch.dict(
            os.environ,
            {"INFLUENCER_REFERENCE_URLS": "https://example.com/env-person.jpg"},
        ):
            resolved = resolve_influencer_image_urls(
                {"visual_mode": "product_only"}
            )

        self.assertEqual(resolved, ())

    def test_explicit_product_only_rejects_contradictory_reference(self):
        with self.assertRaisesRegex(ValueError, "보낼 수 없습니다"):
            resolve_influencer_image_urls(
                {
                    "visual_mode": "product_only",
                    "influencer_image_urls": [
                        "https://example.com/person.jpg"
                    ],
                }
            )

    def test_generated_model_uses_no_influencer_reference(self):
        with patch.dict(
            os.environ,
            {"INFLUENCER_REFERENCE_URLS": "https://example.com/env-person.jpg"},
        ):
            resolved = resolve_influencer_image_urls(
                {"visual_mode": "generated_model"}
            )

        self.assertEqual(resolved, ())

    def test_generated_model_rejects_contradictory_reference(self):
        with self.assertRaisesRegex(ValueError, "보낼 수 없습니다"):
            resolve_influencer_image_urls(
                {
                    "visual_mode": "generated_model",
                    "influencer_image_urls": [
                        "https://example.com/person.jpg"
                    ],
                }
            )

    def test_explicit_model_included_requires_resolved_reference(self):
        with patch.dict(os.environ, {"INFLUENCER_REFERENCE_URLS": ""}):
            with self.assertRaisesRegex(ValueError, "필요합니다"):
                resolve_influencer_image_urls(
                    {"visual_mode": "model_included"}
                )

    def test_legacy_request_keeps_server_default_influencer_behavior(self):
        with patch.dict(
            os.environ,
            {"INFLUENCER_REFERENCE_URLS": "https://example.com/env-person.jpg"},
        ):
            resolved = resolve_influencer_image_urls({})

        self.assertEqual(
            resolved,
            ("https://example.com/env-person.jpg",),
        )

    def test_infers_duration_from_script_when_missing(self):
        self.assertEqual(
            _script_duration_seconds({"video": {"video_duration": "6"}}),
            6,
        )

    def test_ignores_invalid_script_duration(self):
        self.assertIsNone(_script_duration_seconds({"video": {"video_duration": "15s"}}))

    def test_status_metadata_identifies_script_provider_failure(self):
        self.assertEqual(
            _error_metadata(
                "FAILED",
                "SCRIPT_GENERATION",
                "OpenRouter 요청이 거부되었습니다. HTTP 404: No endpoints available",
            ),
            ("SCRIPT_PROVIDER_UNAVAILABLE", True),
        )

    def test_status_metadata_identifies_video_input_failure(self):
        self.assertEqual(
            _error_metadata("FAILED", "VIDEO_GENERATION", "이미지 format is not supported"),
            ("VIDEO_INPUT_INVALID", False),
        )

    def test_status_message_identifies_script_regeneration(self):
        self.assertEqual(
            _status_message("PROCESSING", "SCRIPT_REGENERATION"),
            "음성 길이에 맞게 스크립트를 다시 생성하고 있습니다.",
        )

    def test_failure_keeps_the_stage_that_failed(self):
        self.assertEqual(
            _error_metadata(
                "FAILED",
                "TTS_GENERATION",
                "3번째 장면 음성이 너무 깁니다.",
            ),
            ("TTS_SCENE_TOO_LONG", True),
        )

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
        self.assertEqual(pipeline_class.call_args.kwargs["max_retries"], 0)
        self.assertTrue(pipeline_class.call_args.kwargs["production_mode"])
        self.assertEqual(
            pipeline_class.call_args.kwargs["square_output_strategy"],
            "reject",
        )

    @patch("app.api.v1.final_generation.VideoValidationPipeline")
    @patch("app.api.v1.final_generation.select_video_resolution", return_value="1080p")
    @patch("app.api.v1.final_generation.build_video_client")
    @patch("app.api.v1.final_generation.get_video_model_capabilities")
    def test_product_only_generation_passes_explicit_center_crop_strategy(
        self,
        get_capabilities,
        _build_client,
        _select_resolution,
        pipeline_class,
    ):
        get_capabilities.return_value = VideoModelCapabilities(
            model_id="video-model",
            name="Video",
            supported_durations=(4,),
            supported_aspect_ratios=("9:16",),
            supported_resolutions=("1080p",),
            generate_audio=False,
        )
        pipeline_class.return_value.run.return_value = Mock()

        _generate_video(
            script={"scenes": [{"time_range_sec": {"start": 0, "end": 4}}]},
            image_url="https://example.com/product.jpg",
            influencer_image_url=None,
            detail_image_urls=(),
            service=None,
            square_output_strategy="center_crop",
        )

        self.assertEqual(
            pipeline_class.call_args.kwargs["square_output_strategy"],
            "center_crop",
        )

    def test_center_crop_strategy_rejects_influencer_reference_before_provider(self):
        with self.assertRaisesRegex(ValueError, "product-only"):
            _generate_video(
                script={"scenes": [{"time_range_sec": {"start": 0, "end": 4}}]},
                image_url="https://example.com/product.jpg",
                influencer_image_url=("https://example.com/person.jpg",),
                detail_image_urls=(),
                service=None,
                square_output_strategy="center_crop",
            )

    @patch("app.api.v1.final_generation.update_candidate")
    @patch(
        "app.api.v1.final_generation.get_job",
        return_value={
            "candidates": [
                {"candidate_id": "candidate-01", "status": "PENDING"},
                {"candidate_id": "candidate-02", "status": "PROCESSING"},
                {"candidate_id": "candidate-03", "status": "COMPLETED"},
            ]
        },
    )
    def test_shared_failure_marks_every_unresolved_candidate_failed(
        self, _get_job, update_candidate
    ):
        _fail_unresolved_candidates("job-1", "TTS_GENERATION", RuntimeError("tts failed"))

        self.assertEqual(update_candidate.call_count, 2)
        self.assertEqual(
            {call.args[1] for call in update_candidate.call_args_list},
            {"candidate-01", "candidate-02"},
        )

    @patch(
        "app.api.v1.final_generation.get_job",
        return_value={
            "job_id": "job-1",
            "status": "PARTIAL_COMPLETED",
            "output_path": "runtime/final/job-1/candidate-01.mp4",
            "candidates": [
                {
                    "candidate_id": "candidate-01",
                    "status": "COMPLETED",
                    "output_path": "runtime/final/job-1/candidate-01.mp4",
                },
                {
                    "candidate_id": "candidate-02",
                    "status": "FAILED",
                    "output_path": None,
                },
            ],
        },
    )
    def test_partial_status_exposes_candidate_urls_without_private_paths(self, _get_job):
        payload = get_generation_status("job-1")

        self.assertEqual(payload["status"], "PARTIAL_COMPLETED")
        self.assertNotIn("output_path", payload)
        self.assertNotIn("output_path", payload["candidates"][0])
        self.assertIn("candidate-01/file", payload["candidates"][0]["video_url"])
        self.assertIn("/generate/job-1/file", payload["video_url"])

    @patch("app.api.v1.final_generation.run_candidate_retry")
    @patch("app.api.v1.final_generation.update_job")
    @patch("app.api.v1.final_generation.update_candidate")
    @patch("app.api.v1.final_generation.Path.is_file", return_value=True)
    @patch(
        "app.api.v1.final_generation.get_job_payload",
        return_value={"product": {"name": "상품"}, "script": {"scenes": []}},
    )
    @patch(
        "app.api.v1.final_generation.get_job",
        return_value={
            "job_id": "job-1",
            "candidates": [
                {
                    "candidate_id": "candidate-01",
                    "status": "FAILED",
                    "cost": 1.25,
                    "attempts": 2,
                }
            ],
        },
    )
    def test_failed_candidate_retry_preserves_prior_cost_for_worker(
        self,
        _get_job,
        _get_payload,
        _is_file,
        _update_candidate,
        _update_job,
        run_retry,
    ):
        background = BackgroundTasks()
        result = retry_candidate("job-1", "candidate-01", background)

        self.assertEqual(result["status"], "PENDING")
        task = background.tasks[0]
        self.assertEqual(task.args[-2:], (1.25, 2))
        self.assertNotIn("cost", _update_candidate.call_args.kwargs)
        self.assertNotIn("attempts", _update_candidate.call_args.kwargs)

    @patch("app.api.v1.final_generation.update_job")
    @patch("app.api.v1.final_generation.update_candidate")
    @patch("app.api.v1.final_generation._generate_video")
    def test_retry_accumulates_lifetime_cost_and_attempts(
        self, generate_video, update_candidate, _update_job
    ):
        generate_video.return_value = Mock(
            status="retry_exhausted",
            job_id="provider-2",
            attempts=1,
            total_cost=0.75,
            validation=Mock(checks={"resolution": {"passed": False}}, errors=["resolution"]),
        )

        _run_candidate(
            job_id="job-1",
            candidate_id="candidate-01",
            payload={"product": {"name": "상품"}},
            script={"scenes": [{"time_range_sec": {"start": 0, "end": 4}}]},
            audio_path=Path("narration.mp3"),
            image_url="https://example.com/product.jpg",
            influencer_image_urls=(),
            service=None,
            prior_cost=1.25,
            prior_attempts=2,
        )

        metadata_call = next(
            call
            for call in update_candidate.call_args_list
            if call.kwargs.get("provider_job_id") == "provider-2"
        )
        self.assertEqual(metadata_call.kwargs["attempts"], 3)
        self.assertEqual(metadata_call.kwargs["cost"], 2.0)

    @patch("app.api.v1.final_generation.shutil.copy2")
    @patch("app.api.v1.final_generation.Path.mkdir")
    @patch("app.api.v1.final_generation.validate_video")
    @patch("app.api.v1.final_generation.read_video_metadata")
    @patch("app.api.v1.final_generation.render_captioned_video_file")
    @patch("app.api.v1.final_generation.combine_video_and_audio")
    @patch("app.api.v1.final_generation.update_job")
    @patch("app.api.v1.final_generation.update_candidate")
    @patch("app.api.v1.final_generation._generate_video")
    def test_completed_candidate_persists_raw_and_normalized_source_evidence(
        self,
        generate_video,
        update_candidate,
        _update_job,
        _combine,
        render_caption,
        read_metadata,
        validate_final,
        _mkdir,
        _copy,
    ):
        raw_checks = {
            "aspect_ratio": {"passed": False},
            "resolution": {"passed": False},
        }
        normalized_checks = {
            "aspect_ratio": {"passed": True},
            "resolution": {"passed": True},
        }
        final_checks = {"aspect_ratio": {"passed": True}}
        generate_video.return_value = PipelineResult(
            status=PipelineStatus.COMPLETED,
            attempts=1,
            job_id="provider-1",
            video_url="/video/file",
            validation=ValidationResult(True, normalized_checks, []),
            provider_validation=ValidationResult(
                False, raw_checks, ["aspect_ratio", "resolution"]
            ),
            total_cost=1.0,
            storage_path="runtime/videos/provider-1/final.mp4",
            source_normalized=True,
            normalization_strategy="center_crop",
            source_metadata=VideoMetadata(1440, 1440, 4.0),
            normalized_metadata=VideoMetadata(1080, 1920, 4.0),
        )
        render_caption.return_value = {
            "job_id": "caption-1",
            "output_path": "runtime/captioned.mp4",
        }
        read_metadata.return_value = VideoMetadata(
            1080,
            1920,
            4.0,
            fps=30.0,
            codec="h264",
            bitrate=8_000_000,
            black_frame_ratio=0.0,
        )
        validate_final.return_value = ValidationResult(True, final_checks, [])

        _run_candidate(
            job_id="job-1",
            candidate_id="candidate-01",
            payload={
                "product": {"name": "상품"},
                "square_output_strategy": "center_crop",
            },
            script={"scenes": [{"time_range_sec": {"start": 0, "end": 4}}]},
            audio_path=Path("narration.mp3"),
            image_url="https://example.com/product.jpg",
            influencer_image_urls=(),
            service=None,
        )

        completed = next(
            call
            for call in update_candidate.call_args_list
            if call.kwargs.get("status") == "COMPLETED"
        )
        evidence = completed.kwargs["validation"]
        self.assertEqual(evidence["checks"], final_checks)
        self.assertEqual(evidence["provider_checks"], raw_checks)
        self.assertEqual(evidence["normalized_checks"], normalized_checks)
        self.assertTrue(evidence["source_normalized"])
        self.assertEqual(evidence["normalization_strategy"], "center_crop")
        self.assertEqual(
            (evidence["source_width"], evidence["source_height"]),
            (1440, 1440),
        )
        self.assertEqual(
            (evidence["normalized_width"], evidence["normalized_height"]),
            (1080, 1920),
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

    def test_allows_product_only_generation(self):
        body = FinalGenerationBody(
            product={"name": "상품"},
            script={"meta": {}, "summary": {}, "scenes": []},
            image_url="https://example.com/product.jpg",
        )

        self.assertIsNone(body.influencer_image_url)
        self.assertEqual(body.influencer_image_urls, [])

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

    @patch("app.api.v1.final_generation._generate_script")
    @patch("app.api.v1.final_generation.OpenRouterTTSClient")
    def test_script_regeneration_preserves_initial_duration_when_payload_omits_it(
        self, tts_client_class, generate_script
    ):
        initial_script = {"video": {"video_duration": "6"}, "scenes": []}
        tts_client_class.return_value.generate_narration.side_effect = [
            SceneAudioDurationError(1, 2.0, 2.45),
            b"valid-audio",
        ]
        generate_script.return_value = initial_script

        _generate_narration_with_script_regeneration(
            payload={"product": {"name": "상품"}},
            script=initial_script,
            service=None,
        )

        self.assertEqual(
            generate_script.call_args.args[0]["max_duration_seconds"],
            6,
        )

    @patch("app.api.v1.final_generation.resolve_script_generation_duration", return_value=(4, (4,)))
    @patch("app.api.v1.final_generation.build_script_client")
    def test_tts_regeneration_bounds_script_provider_to_one_attempt(
        self, build_client, _resolve_duration
    ):
        build_client.return_value.generate_script.return_value = {"scenes": []}

        _generate_script(
            {"product": {"name": "상품"}, "max_duration_seconds": 4},
            None,
            retry_error=SceneAudioDurationError(1, 1.0, 1.5),
        )

        self.assertEqual(
            build_client.return_value.generate_script.call_args.kwargs["max_attempts"],
            1,
        )

    @patch("app.api.v1.final_generation._generate_script")
    @patch("app.api.v1.final_generation.OpenRouterTTSClient")
    def test_dialogue_regeneration_failure_uses_bounded_scene_local_fallback(
        self, tts_client_class, generate_script
    ):
        script = {
            "scenes": [
                {
                    "time_range_sec": {"start": 0, "end": 1},
                    "auditory": {"subtitle": None, "voiceover": "성분 확인하세요"},
                }
            ]
        }
        duration_error = SceneAudioDurationError(1, 1.0, 2.0)
        calls = []

        def generate_narration(candidate_script):
            calls.append(candidate_script)
            if len(calls) == 1:
                raise duration_error
            return b"combined"

        tts_client_class.return_value.generate_narration.side_effect = generate_narration
        generate_script.side_effect = ScriptDialogueLengthError(1, 4, 6)

        final_script, audio = _generate_narration_with_script_regeneration(
            payload={"product": {"name": "상품"}},
            script=script,
            service=None,
        )

        self.assertEqual(audio, b"combined")
        self.assertEqual(len(calls), 2)
        self.assertEqual(final_script["scenes"][0]["auditory"]["voiceover"], "성분")
        self.assertEqual(
            final_script["scenes"][0]["auditory"]["subtitle"],
            "성분 확인하세요",
        )
        self.assertEqual(final_script["scenes"][0]["time_range_sec"], {"start": 0, "end": 1})

    def test_persistent_tts_overflow_shortens_then_silences_only_failing_scene(self):
        script = {
            "scenes": [
                {
                    "time_range_sec": {"start": 0, "end": 1},
                    "auditory": {"subtitle": None, "voiceover": "성분 확인하세요"},
                },
                {
                    "time_range_sec": {"start": 1, "end": 4},
                    "auditory": {"subtitle": None, "voiceover": "지금 확인"},
                },
            ]
        }
        calls = []

        def generate_narration(candidate_script):
            calls.append(candidate_script)
            if len(calls) == 1:
                raise SceneAudioDurationError(1, 1.0, 1.5)
            return b"combined"

        client = Mock(generate_narration=generate_narration)
        final_script, audio = _generate_narration_with_deterministic_fallback(
            script,
            client,
            SceneAudioDurationError(1, 1.0, 2.0),
        )

        self.assertEqual(audio, b"combined")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(final_script["scenes"][0]["auditory"]["voiceover"])
        self.assertEqual(
            final_script["scenes"][0]["auditory"]["subtitle"],
            "성분 확인하세요",
        )
        self.assertEqual(
            final_script["scenes"][1]["auditory"],
            {"subtitle": "지금 확인", "voiceover": "지금 확인"},
        )

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
    @patch("app.api.v1.final_generation.validate_normalized_influencer_references")
    @patch("app.api.v1.final_generation.validate_product_image_inputs")
    def test_start_returns_job_id_and_status_url(
        self, _validate_images, _validate_influencer, create_job, run_job
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
        self.assertEqual(payload["candidate_count"], 3)
        self.assertIn(payload["job_id"], payload["status_url"])
        create_job.assert_called_once()
        self.assertEqual(
            create_job.call_args.kwargs["payload"]["square_output_strategy"],
            "reject",
        )
        run_job.assert_called_once()

    @patch("app.api.v1.final_generation.run_generation_job")
    @patch("app.api.v1.final_generation.create_job")
    @patch("app.api.v1.final_generation.validate_normalized_influencer_references")
    @patch("app.api.v1.final_generation.validate_product_image_inputs")
    @patch("app.api.v1.final_generation.resolve_influencer_image_urls", return_value=())
    def test_start_persists_explicit_product_only_center_crop_strategy(
        self,
        _resolve_influencer,
        _validate_images,
        _validate_influencer,
        create_job,
        _run_job,
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
                    "square_output_strategy": "center_crop",
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            create_job.call_args.kwargs["payload"]["square_output_strategy"],
            "center_crop",
        )

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
