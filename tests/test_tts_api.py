import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app.api.v1.tts import TTSGenerationBody, generate_narration
from app.tts_generator import SceneAudioDurationError


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

    @patch(
        "app.api.v1.tts.GoogleTTSClient",
        return_value=type(
            "FailingTTSClient",
            (),
            {
                "generate_narration": lambda _self, _script: (_ for _ in ()).throw(
                    SceneAudioDurationError(2, 3.0, 3.4)
                )
            },
        )(),
    )
    def test_returns_script_regeneration_signal_when_scene_audio_is_too_long(self, _client):
        with self.assertRaises(HTTPException) as context:
            generate_narration(TTSGenerationBody(script={"scenes": []}))

        self.assertEqual(context.exception.status_code, 422)
        self.assertEqual(context.exception.detail["retryable"], True)
        self.assertEqual(context.exception.detail["next_step"], "regenerate_script")
        self.assertEqual(context.exception.detail["scene_number"], 2)


if __name__ == "__main__":
    unittest.main()
