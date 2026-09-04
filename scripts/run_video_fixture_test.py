#!/usr/bin/env python3
"""Run a small, billable end-to-end fixture test against the deployed API.

Each case generates one script and one final video. Keep the case count small.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from run_script_fixture_test import DEFAULT_SOURCES, load_products, request_json


INFLUENCER_IMAGE_URL = "https://lh3.googleusercontent.com/d/1enbiDWV-2TBqDlXNjCOL0WzgPrfR9UGv"
RECOMMENDED_PRODUCT_IDS = (
    "13b6fc03-7411-4a3e-8a3c-ebc46e98b0a1",
    "0c2da901-2179-4d92-99bd-af805ace36a4",
    "e43de954-52be-40a1-982a-0516c8937020",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://reels.quedot.kr")
    parser.add_argument("--duration", type=int, default=6, choices=(4, 6, 8, 10, 15))
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/quedot-video-fixture-results.json"),
    )
    return parser.parse_args()


def select_products() -> list[dict[str, Any]]:
    products = load_products(list(DEFAULT_SOURCES))
    by_id = {item["product"].get("product_id"): item for item in products}
    selected = [by_id[product_id] for product_id in RECOMMENDED_PRODUCT_IDS if product_id in by_id]
    if len(selected) != len(RECOMMENDED_PRODUCT_IDS):
        missing = set(RECOMMENDED_PRODUCT_IDS) - set(by_id)
        raise ValueError(f"fixture products not found: {sorted(missing)}")
    return selected


def poll_job(base_url: str, status_url: str, timeout: float, poll_interval: float) -> dict[str, Any]:
    started = time.monotonic()
    absolute_url = urljoin(base_url.rstrip("/") + "/", status_url)
    latest: dict[str, Any] = {}
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            return {
                "status": "TIMEOUT",
                "elapsed_seconds": round(elapsed, 2),
                "last_response": latest,
            }
        _, response = request_json("GET", absolute_url)
        if not isinstance(response, dict):
            return {"status": "FAILED", "elapsed_seconds": round(time.monotonic() - started, 2), "error": response}
        latest = response
        if response.get("status") in {"COMPLETED", "FAILED"}:
            return {
                "status": response.get("status"),
                "stage": response.get("stage"),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "error": response.get("error"),
                "error_code": response.get("error_code"),
                "video_url": response.get("video_url"),
                "download_url": response.get("download_url"),
                "response": response,
            }
        time.sleep(poll_interval)


def run_case(base_url: str, item: dict[str, Any], duration: int, timeout: float, poll_interval: float) -> dict[str, Any]:
    product = item["product"]
    started = time.monotonic()
    result: dict[str, Any] = {
        "product_id": product.get("product_id"),
        "product_name": product.get("name"),
        "source": item["source"],
        "duration_seconds": duration,
    }
    script_payload = {
        "product": product,
        "image_url": product.get("image_url"),
        "prompt": "광고 목적: 판매\nCTA: 링크 확인",
        "max_duration_seconds": duration,
        "channel": "Instagram Reels",
    }
    status_code, script_start = request_json(
        "POST", urljoin(base_url.rstrip("/") + "/", "api/v1/reels/script"), script_payload
    )
    result["script_initial_http_status"] = status_code
    if not isinstance(script_start, dict) or not script_start.get("job_id"):
        result.update({"status": "SCRIPT_FAILED", "error": script_start, "elapsed_seconds": round(time.monotonic() - started, 2)})
        return result

    script_result = poll_job(base_url, script_start["status_url"], timeout, poll_interval)
    result["script"] = {
        "job_id": script_start["job_id"],
        "status": script_result.get("status"),
        "elapsed_seconds": script_result.get("elapsed_seconds"),
        "error": script_result.get("error"),
    }
    script_response = script_result.get("response")
    if script_result.get("status") != "COMPLETED" or not isinstance(script_response, dict) or not isinstance(script_response.get("script"), dict):
        result.update({"status": "SCRIPT_FAILED", "error": script_result.get("error") or script_result, "elapsed_seconds": round(time.monotonic() - started, 2)})
        return result

    video_payload = {
        "product": product,
        "script": script_response["script"],
        "image_url": product.get("image_url"),
        "influencer_image_url": INFLUENCER_IMAGE_URL,
        "max_duration_seconds": duration,
    }
    video_status_code, video_start = request_json(
        "POST", urljoin(base_url.rstrip("/") + "/", "api/v1/reels/generate"), video_payload
    )
    result["video_initial_http_status"] = video_status_code
    if not isinstance(video_start, dict) or not video_start.get("job_id"):
        result.update({"status": "VIDEO_FAILED", "error": video_start, "elapsed_seconds": round(time.monotonic() - started, 2)})
        return result

    video_result = poll_job(base_url, video_start["status_url"], timeout, poll_interval)
    result.update(
        {
            "status": video_result.get("status"),
            "stage": video_result.get("stage"),
            "video_job_id": video_start["job_id"],
            "video_elapsed_seconds": video_result.get("elapsed_seconds"),
            "error": video_result.get("error"),
            "error_code": video_result.get("error_code"),
            "video_url": video_result.get("video_url"),
            "download_url": video_result.get("download_url"),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    )
    return result


def main() -> int:
    args = parse_args()
    try:
        selected = select_products()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    print(f"Testing {len(selected)} video fixtures against {args.base_url}")
    results = []
    for index, item in enumerate(selected, start=1):
        result = run_case(args.base_url, item, args.duration, args.timeout, args.poll_interval)
        results.append(result)
        print(f"[{index}/{len(selected)}] {result['product_name']} -> {result.get('status')} ({result.get('elapsed_seconds', 0)}s)")

    summary = {
        "base_url": args.base_url,
        "duration_seconds": args.duration,
        "fixture_count": len(results),
        "completed": sum(result.get("status") == "COMPLETED" for result in results),
        "failed": sum(result.get("status") not in {"COMPLETED", "TIMEOUT"} for result in results),
        "timeouts": sum(result.get("status") == "TIMEOUT" for result in results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results written to {args.output}")
    return 0 if summary["failed"] == 0 and summary["timeouts"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
