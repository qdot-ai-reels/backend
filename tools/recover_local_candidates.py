#!/usr/bin/env python3
"""Finish paid provider candidates from retained local MP4 files without a new POST."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from app.api.v1.caption import render_captioned_video_file
from app.api.v1.final_generation import (
    LOCAL_COMBINED_OUTPUT_DIR,
    _finalize_candidate_job,
    _source_normalization_evidence,
)
from app.api.v1.video import publish_validated_video
from app.db import init_db
from app.generation_jobs import (
    create_job,
    get_job,
    get_job_payload,
    update_candidate,
    update_job,
)
from app.media_combiner import combine_video_and_audio
from app.video_generator import VideoGenerationRequest, VideoGenerationResult
from app.video_metadata import read_video_metadata
from app.video_validation_pipeline import (
    PipelineStatus,
    SquareOutputStrategy,
    VideoValidationPipeline,
)
from app.video_validator import ValidationPolicy, ValidationResult, validate_video


RECOVERY_ROOT = Path("runtime/provider-recovery").resolve()
FINAL_ROOT = Path("runtime/final")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        metavar="CANDIDATE_ID:PROVIDER_JOB_ID:ATTEMPTS:COST_USD:LOCAL_MP4",
        help="Repeat once per failed candidate to recover",
    )
    parser.add_argument(
        "--bootstrap-report",
        type=Path,
        help="Create a missing local job from a retained production smoke report",
    )
    parser.add_argument(
        "--rerender-completed",
        action="store_true",
        help=(
            "Re-run local mux/caption validation for a completed candidate while "
            "preserving its provider job, attempts, and cost"
        ),
    )
    return parser.parse_args()


def parse_candidate(value: str) -> tuple[str, str, int, float, Path]:
    parts = value.split(":", 4)
    if len(parts) != 5 or not all(part.strip() for part in parts):
        raise SystemExit(f"Invalid --candidate value: {value}")
    candidate_id, provider_job_id, raw_attempts, raw_cost, raw_path = parts
    try:
        attempts = int(raw_attempts)
        cost = float(raw_cost)
    except ValueError as error:
        raise SystemExit(f"Invalid attempts or cost in --candidate: {value}") from error
    if attempts < 1 or cost < 0:
        raise SystemExit(f"Attempts must be positive and cost non-negative: {value}")
    path = Path(raw_path).resolve()
    if RECOVERY_ROOT != path and RECOVERY_ROOT not in path.parents:
        raise SystemExit(f"Recovery source must be under {RECOVERY_ROOT}: {path}")
    if not path.is_file():
        raise SystemExit(f"Recovery source does not exist: {path}")
    return candidate_id, provider_job_id, attempts, cost, path


def bootstrap_job(
    job_id: str,
    report_path: Path,
    candidates: list[tuple[str, str, int, float, Path]],
) -> None:
    """Recreate only the retained local job when the container DB is unavailable."""
    report = json.loads(report_path.read_text())
    request = report.get("request") or {}
    script_job = report.get("script") or {}
    script = script_job.get("script")
    image_url = request.get("image_url")
    product_id = request.get("product_id")
    product_name = request.get("product_name")
    if not isinstance(script, dict) or not all(
        isinstance(value, str) and value
        for value in (image_url, product_id, product_name)
    ):
        raise SystemExit(f"Bootstrap report is missing job inputs: {report_path}")

    product = {
        "product_id": product_id,
        "name": product_name,
        "image_url": image_url,
        "detail_image_urls": [],
        "category_group": ["유아 식품"],
        "selling_point": "사과주스를 담은 스파우트 파우치 대표 단품 이미지",
    }
    payload = {
        "product": product,
        "script": script,
        "image_url": image_url,
        "influencer_image_url": None,
        "influencer_image_urls": [],
        "reviews": [],
        "prompt": None,
        "max_duration_seconds": request.get("duration_seconds") or 4,
        "channel": "Instagram Reels",
        "target_audience": "스파우트 파우치 사과주스를 찾는 보호자",
        "candidate_count": len(candidates),
        "square_output_strategy": "center_crop",
    }
    create_job(
        job_id,
        input_type="product_and_script",
        product=product,
        script=script,
        image_url=image_url,
        payload=payload,
        candidate_count=len(candidates),
    )
    for candidate_id, provider_job_id, attempts, cost, _path in candidates:
        update_candidate(
            job_id,
            candidate_id,
            expected_status="PENDING",
            status="FAILED",
            stage="VIDEO_GENERATION",
            provider_job_id=provider_job_id,
            attempts=attempts,
            cost=cost,
            error="Provider output retained for explicit local format recovery",
            error_code="PROVIDER_OUTPUT_SHAPE_INVALID",
            retryable=False,
        )
    update_job(
        job_id,
        status="FAILED",
        stage="FAILED",
        cost=sum(candidate[3] for candidate in candidates),
        error_message="Provider returned square media for a portrait request",
    )


def quality_payload(
    *,
    video_result: Any,
    final_validation: ValidationResult,
    final_metadata: Any,
) -> dict[str, Any]:
    provider_validation = video_result.provider_validation or video_result.validation
    return {
        "passed": final_validation.is_valid,
        "checks": final_validation.checks,
        "provider_checks": provider_validation.checks,
        "normalized_checks": video_result.validation.checks,
        **_source_normalization_evidence(video_result),
        "recovered_from_retained_provider_source": True,
        "width": final_metadata.width,
        "height": final_metadata.height,
        "duration_seconds": final_metadata.duration_seconds,
        "fps": final_metadata.fps,
        "codec": final_metadata.codec,
        "bitrate_kbps": (
            round(final_metadata.bitrate / 1000)
            if final_metadata.bitrate is not None
            else None
        ),
        "black_frame_ratio": final_metadata.black_frame_ratio,
        "technical_score": round(
            100
            * sum(check["passed"] for check in final_validation.checks.values())
            / max(1, len(final_validation.checks))
        ),
    }


def recover_one(
    *,
    job_id: str,
    candidate_id: str,
    provider_job_id: str,
    source_path: Path,
    script: dict[str, Any],
    image_url: str,
    rerender_completed: bool = False,
) -> None:
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"Generation job not found: {job_id}")
    candidate = next(
        (item for item in job.get("candidates", []) if item.get("candidate_id") == candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError(f"Candidate not found: {candidate_id}")
    prior_status = str(candidate.get("status") or "")
    if prior_status != "FAILED" and not (
        rerender_completed and prior_status == "COMPLETED"
    ):
        raise ValueError(
            f"Only FAILED candidates can be recovered by default: {candidate_id}"
        )
    if candidate.get("provider_job_id") != provider_job_id:
        raise ValueError(
            f"Provider provenance mismatch for {candidate_id}: "
            f"expected {candidate.get('provider_job_id')}, got {provider_job_id}"
        )

    prior_attempts = int(candidate.get("attempts") or 0)
    prior_cost = float(candidate.get("cost") or 0.0)
    update_candidate(
        job_id,
        candidate_id,
        expected_status=prior_status,
        status="PROCESSING",
        stage="VIDEO_GENERATION",
        error=None,
        error_code=None,
        retryable=None,
    )
    update_job(job_id, status="PROCESSING", stage="VIDEO_GENERATION", error_message=None)

    stage = "VIDEO_GENERATION"
    try:
        video_result = VideoValidationPipeline(
            generate_video=lambda _request, _attempt: VideoGenerationResult(
                job_id=provider_job_id,
                status="completed",
                video_url=f"retained-local://{candidate_id}",
                cost=0.0,
            ),
            download_video=lambda _url, destination: shutil.copy2(source_path, destination),
            publish_video=publish_validated_video,
            max_retries=0,
            production_mode=True,
            square_output_strategy=SquareOutputStrategy.CENTER_CROP,
        ).run(
            VideoGenerationRequest(
                script=script,
                image_url=image_url,
                resolution="1080p",
                aspect_ratio="9:16",
                generate_audio=False,
            )
        )
        if video_result.status != PipelineStatus.COMPLETED or not video_result.storage_path:
            failed = ", ".join(video_result.validation.errors) or "unknown"
            raise RuntimeError(f"Recovered provider video failed validation: {failed}")

        stage = "AUDIO_MERGE"
        update_candidate(
            job_id,
            candidate_id,
            stage=stage,
            validation={
                "passed": None,
                "checks": video_result.validation.checks,
                "provider_checks": (
                    video_result.provider_validation or video_result.validation
                ).checks,
                **_source_normalization_evidence(video_result),
                "recovered_from_retained_provider_source": True,
            },
        )
        update_job(job_id, stage=stage)
        audio_path = Path("runtime/tts") / job_id / "narration.mp3"
        combined_path = LOCAL_COMBINED_OUTPUT_DIR / job_id / candidate_id / "combined.mp4"
        combine_video_and_audio(video_result.storage_path, audio_path, combined_path)

        stage = "CAPTION_RENDER"
        update_candidate(job_id, candidate_id, stage=stage)
        update_job(job_id, stage=stage)
        caption_result = render_captioned_video_file(script, combined_path)
        rendered_path = Path(str(caption_result["output_path"]))
        expected_duration = float(script["scenes"][-1]["time_range_sec"]["end"])
        final_metadata = read_video_metadata(rendered_path)
        final_validation = validate_video(
            final_metadata,
            ValidationPolicy.production(expected_duration),
        )
        validation = quality_payload(
            video_result=video_result,
            final_validation=final_validation,
            final_metadata=final_metadata,
        )
        if not final_validation.is_valid:
            failed = ", ".join(final_validation.errors) or "unknown"
            raise RuntimeError(f"Recovered final video failed validation: {failed}")

        final_directory = FINAL_ROOT / job_id
        final_directory.mkdir(parents=True, exist_ok=True)
        final_path = final_directory / f"{candidate_id}.mp4"
        shutil.copy2(rendered_path, final_path)
        update_candidate(
            job_id,
            candidate_id,
            status="COMPLETED",
            stage="COMPLETED",
            provider_job_id=provider_job_id,
            caption_job_id=str(caption_result["job_id"]),
            output_path=str(final_path),
            attempts=prior_attempts,
            cost=prior_cost,
            validation=validation,
            error=None,
            error_code=None,
            retryable=False,
        )
    except Exception as error:
        update_candidate(
            job_id,
            candidate_id,
            status="FAILED",
            stage=stage,
            attempts=prior_attempts,
            cost=prior_cost,
            error=str(error),
            error_code="LOCAL_RECOVERY_FAILED",
            retryable=False,
        )
        raise


def main() -> int:
    args = parse_args()
    init_db()
    candidates = [parse_candidate(value) for value in args.candidate]
    job = get_job(args.job_id)
    if job is None and args.bootstrap_report is not None:
        bootstrap_job(
            args.job_id,
            args.bootstrap_report.resolve(),
            candidates,
        )
        job = get_job(args.job_id)
    payload = get_job_payload(args.job_id)
    if job is None or payload is None:
        raise SystemExit(f"Generation job or private payload not found: {args.job_id}")
    script = job.get("script")
    image_url = payload.get("image_url")
    if not isinstance(script, dict) or not isinstance(image_url, str) or not image_url:
        raise SystemExit("The job is missing its script or product image URL")

    recovered = []
    try:
        for candidate_id, provider_job_id, _attempts, _cost, source_path in candidates:
            recover_one(
                job_id=args.job_id,
                candidate_id=candidate_id,
                provider_job_id=provider_job_id,
                source_path=source_path,
                script=script,
                image_url=image_url,
                rerender_completed=args.rerender_completed,
            )
            recovered.append(candidate_id)
    finally:
        _finalize_candidate_job(args.job_id, script)
    final_job = get_job(args.job_id)
    print(
        json.dumps(
            {
                "job_id": args.job_id,
                "status": final_job.get("status") if final_job else None,
                "recovered_candidates": recovered,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
