import unittest
from unittest.mock import patch

from app.api.v1.tts import TTSGenerationBody, generate_narration


class FakeTTSClient:
    def generate_narration(self, script):
        return b"fake-mp3-bytes"


class TTSApiTests(unittest.TestCase):
    @patch("app.api.v1.tts.GoogleTTSClient", return_value=FakeTTSClient())
    def test_returns_one_mp3_response_for_complete_script(self, _client):
        response = generate_narration(
            TTSGenerationBody(
                script={
                    "scenes": [
                        {"voiceover": "첫 장면"},
                        {"voiceover": "마지막 장면"},
                    ]
                }
            )
        )

        self.assertEqual(response.body, b"fake-mp3-bytes")
        self.assertEqual(response.media_type, "audio/mpeg")
        self.assertIn("narration.mp3", response.headers["content-disposition"])


if __name__ == "__main__":
    unittest.main()
