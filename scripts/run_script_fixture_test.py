#!/usr/bin/env python3
"""Run the script-generation fixture set against a configurable API base URL.

This intentionally calls only the script API. Video generation is excluded because
it creates billable external jobs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES = (
    REPO_ROOT / "자료" / "quedot-gonggu-sample.json",
    REPO_ROOT / "자료" / "quedot-gonggu-sample-extra-3.json",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="https://reels.quedot.kr",
        help="API base URL (default: deployed backend)",
    )
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--duration", type=int, default=6, choices=(4, 6, 8, 10, 15))
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument(
        "--timeout",
        type=float,
        default=240.0,
        help="Maximum seconds to wait for each script job",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/quedot-script-fixture-results.json"),
    )
    parser.add_argument(
        "--source",
        type=Path,
        action="append",
        help="Fixture JSON source; may be supplied more than once",
    )
    return parser.parse_args()


def load_products(sources: list[Path]) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for source in sources:
        with source.open(encoding="utf-8") as file:
            document = json.load(file)
        for event in document.get("events", []):
            for product in event.get("products", []):
                product_id = str(product.get("product_id", ""))
                if not product_id or product_id in seen_ids:
                    continue
                seen_ids.add(product_id)
                products.append(
                    {
                        "product": product,
                        "source": source.name,
                        "event_name": event.get("event_name"),
                    }
                )
    return products


def fixture_score(item: dict[str, Any]) -> tuple[int, str]:
    """Prefer fixtures that exercise different image and data conditions."""
    product = item["product"]
    details = product.get("detail_image_urls") or []
    image_urls = [str(product.get("image_url") or "")] + [str(url) for url in details]
    score = 0
    if not product.get("image_url"):
        score += 100
    if not details:
        score += 80
    if any(url.lower().split("?")[0].endswith(".gif") for url in image_urls):
        score += 60
    if any(url.startswith("http://") for url in image_urls):
        score += 50
    if not product.get("usp") and not product.get("selling_point"):
        score += 20
    score += min(len(details), 10)
    return (-score, str(product.get("product_id", "")))


def request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            detail: Any = json.loads(raw)
        except json.JSONDecodeError:
            detail = raw
        return error.code, detail


def run_fixture(
    base_url: str,
    item: dict[str, Any],
    duration: int,
    poll_interval: float,
    timeout: float,
) -> dict[str, Any]:
    product = item["product"]
    payload = {
        "product": product,
        "image_url": product.get("image_url"),
        "prompt": "광고 목적: 판매\nCTA: 링크 확인",
        "max_duration_seconds": duration,
        "channel": "Instagram Reels",
    }
    started = time.monotonic()
    result: dict[str, Any] = {
        "product_id": product.get("product_id"),
        "product_name": product.get("name"),
        "source": item["source"],
        "request": {
            "image_url": product.get("image_url"),
            "detail_image_count": len(product.get("detail_image_urls") or []),
            "max_duration_seconds": duration,
        },
    }

    try:
        status_code, response = request_json(
            "POST", urljoin(base_url.rstrip("/") + "/", "api/v1/reels/script"), payload
        )
        result["initial_http_status"] = status_code
        if not isinstance(response, dict) or not response.get("job_id"):
            result.update({"status": "FAILED", "error": response})
            return result

        result["job_id"] = response["job_id"]
        status_url = urljoin(base_url.rstrip("/") + "/", response.get("status_url", ""))
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                result.update({"status": "TIMEOUT", "elapsed_seconds": round(elapsed, 2)})
                return result
            _, status_response = request_json("GET", status_url)
            if not isinstance(status_response, dict):
                result.update({"status": "FAILED", "error": status_response})
                return result
            state = status_response.get("status")
            result["last_status"] = state
            if state in {"COMPLETED", "FAILED"}:
                result.update(
                    {
                        "status": state,
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                        "error": status_response.get("error"),
                        "error_code": status_response.get("error_code"),
                        "scene_count": len((status_response.get("script") or {}).get("scenes", [])),
                    }
                )
                return result
            time.sleep(poll_interval)
    except (OSError, URLError, TimeoutError) as error:
        result.update({"status": "FAILED", "error": f"{type(error).__name__}: {error}"})
        return result


def main() -> int:
    args = parse_args()
    if args.count < 1:
        print("--count must be at least 1", file=sys.stderr)
        return 2
    sources = args.source or list(DEFAULT_SOURCES)
    products = load_products(sources)
    selected = sorted(products, key=fixture_score)[: args.count]
    print(f"Testing {len(selected)} script fixtures against {args.base_url}")

    results = []
    for index, item in enumerate(selected, start=1):
        result = run_fixture(args.base_url, item, args.duration, args.poll_interval, args.timeout)
        results.append(result)
        print(
            f"[{index}/{len(selected)}] {result['product_name']} -> "
            f"{result.get('status')} ({result.get('elapsed_seconds', 0)}s)"
        )

    summary = {
        "base_url": args.base_url,
        "duration_seconds": args.duration,
        "fixture_count": len(results),
        "completed": sum(result.get("status") == "COMPLETED" for result in results),
        "failed": sum(result.get("status") == "FAILED" for result in results),
        "timeouts": sum(result.get("status") == "TIMEOUT" for result in results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results written to {args.output}")
    return 0 if summary["failed"] == 0 and summary["timeouts"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
