import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.combine import combine_router
from app.media_combiner import MediaDurationMismatchError


class CombineApiTests(unittest.TestCase):
    def _client(self):
        app = FastAPI()
        app.include_router(combine_router)
        return TestClient(app)

    @patch("app.api.v1.combine.combine_video_and_audio")
    def test_uploads_video_and_audio_and_returns_final_video_urls(self, combine):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with patch("app.api.v1.combine.LOCAL_COMBINED_OUTPUT_DIR", output_dir):
                def create_output(_video, _audio, output):
                    output.parent.mkdir(parents=True)
                    output.write_bytes(b"final-mp4")

                combine.side_effect = create_output
                with self._client() as client:
                    response = client.post(
                        "/combine",
                        files={
                            "video": ("video.mp4", b"video", "video/mp4"),
                            "audio": ("narration.mp3", b"audio", "audio/mpeg"),
                        },
                    )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["status"], "completed")
            self.assertTrue(payload["video_url"].startswith("/api/v1/reels/combine/"))
            self.assertTrue(Path(payload["storage_path"]).is_file())
            combine.assert_called_once()

    @patch("app.api.v1.combine.combine_video_and_audio")
    def test_returns_structured_error_for_duration_mismatch(self, combine):
        combine.side_effect = MediaDurationMismatchError(15.0, 12.0)
        with self._client() as client:
            response = client.post(
                "/combine",
                files={
                    "video": ("video.mp4", b"video", "video/mp4"),
                    "audio": ("narration.mp3", b"audio", "audio/mpeg"),
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["error_type"], "duration_mismatch")
        self.assertFalse(response.json()["detail"]["retryable"])


if __name__ == "__main__":
    unittest.main()
