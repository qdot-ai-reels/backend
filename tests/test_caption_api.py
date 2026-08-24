import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api.v1.caption import CaptionRenderBody, render_captioned_video
from app.hyperframes_client import HyperFramesRenderError
from app.video_validator import VideoMetadata


def _script():
    return {
        "meta": {
            "output_format_version": "1.0",
            "framework": "Hook-Body-CTA",
            "language": "ko",
        },
        "summary": {
            "main_target": "보호자",
            "pain_point": "고민",
            "product_usp": "장점",
            "key_message": "메시지",
            "tone_and_manner": "광고",
        },
        "scenes": [{
            "scene_name": "Hook",
            "time_range_sec": {"start": 0, "end": 8},
            "visual": "상품",
            "auditory": {"subtitle": "상품 확인", "voiceover": "상품 확인"},
            "notes": "후킹",
        }],
        "compliance_notes": {"avoid": [], "focus": []},
    }


class CaptionApiTests(unittest.TestCase):
    @patch("app.api.v1.caption.HyperFramesClient.render", return_value={"status": "completed"})
    @patch("app.api.v1.caption.read_video_metadata")
    def test_prepares_shared_project_and_requests_render(self, read_metadata, render):
        read_metadata.return_value = VideoMetadata(width=1080, height=1920, duration_seconds=8)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "combined.mp4"
            source.write_bytes(b"video")
            with patch("app.api.v1.caption.WORKSPACE", workspace):
                result = render_captioned_video(
                    CaptionRenderBody(script=_script(), video_filename="combined.mp4")
                )

            project = workspace / result["job_id"]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["subtitle_count"], 1)
            self.assertTrue((project / "combined.mp4").is_file())
            self.assertIn("상품 확인", (project / "index.html").read_text(encoding="utf-8"))
            render.assert_called_once_with(result["job_id"])

    def test_rejects_source_outside_shared_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("app.api.v1.caption.WORKSPACE", Path(directory)):
                with self.assertRaises(Exception):
                    render_captioned_video(
                        CaptionRenderBody(script=_script(), video_filename="../secret.mp4")
                    )

    @patch(
        "app.api.v1.caption.HyperFramesClient.render",
        side_effect=HyperFramesRenderError("runner unavailable"),
    )
    @patch("app.api.v1.caption.read_video_metadata")
    def test_returns_bad_gateway_when_runner_fails(self, read_metadata, _render):
        read_metadata.return_value = VideoMetadata(width=1080, height=1920, duration_seconds=8)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "combined.mp4").write_bytes(b"video")
            with patch("app.api.v1.caption.WORKSPACE", workspace):
                with self.assertRaisesRegex(Exception, "runner unavailable"):
                    render_captioned_video(
                        CaptionRenderBody(script=_script(), video_filename="combined.mp4")
                    )
            self.assertEqual([path.name for path in workspace.iterdir()], ["combined.mp4"])


if __name__ == "__main__":
    unittest.main()
