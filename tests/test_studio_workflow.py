import unittest
from copy import deepcopy
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.final_generation import router, run_generation_job
from app.generation_templates import get_generation_template
from app.script_generator import ScriptGenerationRequest, build_script_prompt


def template_script(template_id="ugc_full_15"):
    template = get_generation_template(template_id)
    return {
        "meta": {"output_format_version": "1.0", "language": "ko"},
        "product": {"usp": "간편한 상품"},
        "customer": {"main_target": "보호자", "pain_point": "휴대가 불편함"},
        "ads": {
            "goal": "상품 소개",
            "cta_action": "링크 확인",
            "channel_platform": "Instagram Reels",
            "ad_planner": {"persona": None},
            "speaker": {"persona": None, "tone": "자연스럽게"},
            "main_target": "보호자",
        },
        "video": {
            "video_duration": f"{template.duration_seconds}초",
            "required_scenes_elements": None,
            "forbidden_scenes_elements": None,
        },
        "scenes": [
            {
                "section": scene.label,
                "time_range_sec": {
                    "start": scene.start_seconds,
                    "end": scene.end_seconds,
                },
                "visual": f"{scene.label} 장면에서 상품을 자연스럽게 보여준다.",
                "auditory": {
                    "subtitle": scene.label,
                    "voiceover": "지금 확인",
                },
                "intent": f"{scene.label} 전달",
                "notes": None,
            }
            for scene in template.scenes
        ],
        "etc": {"additional_information": None, "video_ads_methodology": None},
    }


class StudioWorkflowApiTests(unittest.TestCase):
    def app(self):
        app = FastAPI()
        app.include_router(router)
        return app

    @patch("app.api.v1.final_generation.run_generation_job")
    @patch("app.api.v1.final_generation.create_job")
    @patch("app.api.v1.final_generation.validate_normalized_influencer_references")
    @patch("app.api.v1.final_generation.validate_product_image_inputs")
    def test_template_without_script_queues_one_click_script_workflow(
        self,
        _validate_product,
        _validate_influencer,
        create_job,
        run_job,
    ):
        with TestClient(self.app()) as client:
            response = client.post(
                "/generate",
                json={
                    "product": {"name": "상품"},
                    "image_url": "https://example.com/product.jpg",
                    "template_id": "ugc_full_15",
                    "template_version": 1,
                    "visual_mode": "generated_model",
                    "candidate_count": 1,
                },
            )

        self.assertEqual(response.status_code, 202)
        result = response.json()
        self.assertEqual(result["template"]["duration_seconds"], 15)
        self.assertEqual(result["stage"], "QUEUED")
        self.assertIsNone(create_job.call_args.kwargs["script"])
        queued_payload = create_job.call_args.kwargs["payload"]
        self.assertEqual(queued_payload["max_duration_seconds"], 15)
        self.assertEqual(queued_payload["template"]["id"], "ugc_full_15")
        run_job.assert_called_once()

    @patch("app.api.v1.final_generation.run_generation_job")
    @patch("app.api.v1.final_generation.create_job")
    @patch("app.api.v1.final_generation.validate_normalized_influencer_references")
    @patch("app.api.v1.final_generation.validate_product_image_inputs")
    def test_template_script_mismatch_is_rejected_before_job_or_paid_work(
        self,
        _validate_product,
        _validate_influencer,
        create_job,
        run_job,
    ):
        script = template_script()
        script["scenes"][3]["time_range_sec"]["start"] = 11
        with TestClient(self.app()) as client:
            response = client.post(
                "/generate",
                json={
                    "product": {"name": "상품"},
                    "image_url": "https://example.com/product.jpg",
                    "template_id": "ugc_full_15",
                    "script": script,
                    "visual_mode": "generated_model",
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("time_range_sec", response.json()["detail"])
        create_job.assert_not_called()
        run_job.assert_not_called()

    @patch("app.api.v1.final_generation.run_generation_job")
    @patch("app.api.v1.final_generation.create_job")
    @patch("app.api.v1.final_generation.validate_normalized_influencer_references")
    @patch("app.api.v1.final_generation.validate_product_image_inputs")
    def test_matching_template_script_is_persisted_with_canonical_timeline(
        self,
        _validate_product,
        _validate_influencer,
        create_job,
        _run_job,
    ):
        script = template_script("ugc_quick_6")
        script["scenes"][0]["section"] = "hook"
        with TestClient(self.app()) as client:
            response = client.post(
                "/generate",
                json={
                    "product": {"name": "상품"},
                    "image_url": "https://example.com/product.jpg",
                    "template_id": "ugc_quick_6",
                    "script": script,
                    "visual_mode": "generated_model",
                },
            )

        self.assertEqual(response.status_code, 202)
        persisted = create_job.call_args.kwargs["script"]
        self.assertEqual(persisted["scenes"][0]["section"], "Hook")
        self.assertEqual(
            persisted["scenes"][-1]["time_range_sec"],
            {"start": 4.8, "end": 6.0},
        )

    @patch("app.api.v1.final_generation.run_generation_job")
    @patch("app.api.v1.final_generation.create_job")
    @patch("app.api.v1.final_generation.validate_normalized_influencer_references")
    @patch("app.api.v1.final_generation.validate_product_image_inputs")
    @patch("app.api.v1.final_generation.get_job_idempotency", return_value=None)
    def test_same_client_request_replays_same_job_and_changed_body_conflicts(
        self,
        get_identity,
        _validate_product,
        _validate_influencer,
        create_job,
        run_job,
    ):
        request = {
            "product": {"name": "상품"},
            "image_url": "https://example.com/product.jpg",
            "template_id": "ugc_quick_4",
            "visual_mode": "generated_model",
            "candidate_count": 1,
            "client_request_id": "browser-request-1",
        }
        with patch("app.api.v1.final_generation.get_job", return_value=None), TestClient(
            self.app()
        ) as client:
            first = client.post("/generate", json=request)
            request_hash = create_job.call_args.kwargs["request_hash"]
            existing_job_id = first.json()["job_id"]
            get_identity.return_value = (existing_job_id, request_hash)
            replay = client.post("/generate", json=request)
            changed = client.post(
                "/generate",
                json={**request, "candidate_count": 2},
            )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["job_id"], existing_job_id)
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(create_job.call_count, 1)
        self.assertEqual(run_job.call_count, 1)

    def test_template_catalog_contract(self):
        with TestClient(self.app()) as client:
            response = client.get("/generation-templates")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.json()["items"]],
            ["ugc_quick_4", "ugc_quick_6", "ugc_balanced_8", "ugc_full_15"],
        )

    @patch("app.api.v1.final_generation._selected_video_model_id", return_value="video/model")
    @patch(
        "app.api.v1.final_generation.create_generation_quote",
        return_value={"quote_id": "quote-1", "total": {"expected": 5.7}},
    )
    def test_quote_endpoint_validates_and_passes_the_template_snapshot(
        self, create_quote, _model
    ):
        with TestClient(self.app()) as client:
            response = client.post(
                "/generation-quotes",
                json={
                    "template_id": "ugc_full_15",
                    "template_version": 1,
                    "candidate_count": 2,
                    "visual_mode": "generated_model",
                    "resolution": "1080p",
                },
            )
            invalid = client.post(
                "/generation-quotes",
                json={
                    "template_id": "ugc_full_15",
                    "candidate_count": 5,
                    "visual_mode": "generated_model",
                    "resolution": "720p",
                },
            )

        self.assertEqual(response.status_code, 201)
        spec = create_quote.call_args.args[0]
        self.assertEqual(spec.duration_seconds, 15)
        self.assertEqual(spec.candidate_count, 2)
        self.assertEqual(spec.resolution, "1080p")
        self.assertEqual(invalid.status_code, 422)


class OneClickWorkerTests(unittest.TestCase):
    def test_template_prompt_contains_exact_timeline_and_package_truth_guard(self):
        template = get_generation_template("ugc_full_15")
        prompt = build_script_prompt(
            ScriptGenerationRequest(
                product={"name": "사과주스 30포"},
                max_duration_seconds=15,
                template_scene_plan=template.prompt_scene_plan(),
            )
        )

        self.assertIn("CTA: 12~15초", prompt)
        self.assertIn("포장 수량", prompt)
        self.assertIn("검증되지 않은 포장 수량", prompt)

    @patch("app.api.v1.final_generation.Path.write_bytes")
    @patch("app.api.v1.final_generation.Path.mkdir")
    @patch("app.api.v1.final_generation._finalize_candidate_job")
    @patch("app.api.v1.final_generation._run_candidate")
    @patch(
        "app.api.v1.final_generation._generate_narration_with_script_regeneration",
        return_value=(template_script(), b"audio"),
    )
    @patch("app.api.v1.final_generation._generate_script", return_value=template_script())
    @patch("app.api.v1.final_generation._build_settings_service", return_value=(None, None))
    @patch("app.api.v1.final_generation.update_job")
    def test_worker_generates_script_before_tts_and_video(
        self,
        update_job,
        _build_service,
        generate_script,
        generate_narration,
        run_candidate,
        _finalize,
        _mkdir,
        _write,
    ):
        payload = {
            "product": {"name": "상품"},
            "image_url": "https://example.com/product.jpg",
            "template_id": "ugc_full_15",
            "template_version": 1,
            "max_duration_seconds": 15,
            "visual_mode": "generated_model",
            "candidate_count": 1,
        }

        run_generation_job("job-1", payload)

        generate_script.assert_called_once()
        generate_narration.assert_called_once()
        run_candidate.assert_called_once()
        first_status = update_job.call_args_list[0]
        self.assertEqual(first_status.kwargs["stage"], "SCRIPT_GENERATION")
        self.assertEqual(payload["script"]["scenes"][3]["time_range_sec"]["start"], 12.0)

    @patch(
        "app.api.v1.final_generation.resolve_exact_script_generation_duration",
        return_value=(15, (4, 6, 8, 15)),
    )
    @patch("app.api.v1.final_generation.build_script_client")
    def test_generated_script_is_normalized_to_exact_template_before_tts(
        self,
        build_client,
        _resolve_duration,
    ):
        generated = template_script()
        for scene in generated["scenes"]:
            scene["section"] = "wrong"
            scene["time_range_sec"] = {"start": 0, "end": 1}
        build_client.return_value.generate_script.return_value = generated

        from app.api.v1.final_generation import _generate_script

        result = _generate_script(
            {
                "product": {"name": "상품"},
                "image_url": "https://example.com/product.jpg",
                "template_id": "ugc_full_15",
                "template_version": 1,
                "max_duration_seconds": 15,
            },
            None,
        )

        request = build_client.return_value.generate_script.call_args.args[0]
        self.assertEqual(request.template_scene_plan[-1]["start_seconds"], 12.0)
        self.assertEqual(result["scenes"][3]["section"], "CTA")
        self.assertEqual(result["scenes"][3]["time_range_sec"], {"start": 12.0, "end": 15.0})


if __name__ == "__main__":
    unittest.main()
