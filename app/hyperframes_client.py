"""HTTP client for the internal HyperFrames rendering runner."""

from __future__ import annotations

import json
from urllib import error, request


class HyperFramesRenderError(RuntimeError):
    """Raised when the HyperFrames runner cannot render a composition."""


class HyperFramesClient:
    def __init__(self, base_url: str, timeout_seconds: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def render(self, project_id: str, output_filename: str = "final.mp4") -> dict[str, object]:
        payload = json.dumps(
            {"project_id": project_id, "output_filename": output_filename}
        ).encode("utf-8")
        http_request = request.Request(
            f"{self.base_url}/render",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise HyperFramesRenderError("HyperFrames 렌더링 요청에 실패했습니다.") from exc

        if not isinstance(result, dict) or result.get("status") != "completed":
            raise HyperFramesRenderError(str(result))
        return result
