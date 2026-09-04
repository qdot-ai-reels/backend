import os
import unittest
from unittest.mock import patch

from app.runtime_config import resolve_script_generation_duration
from app.script_generator import OpenRouterClient
from app.settings_service import VideoModelCapabilities
from app.tts_generator import (
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_VOICE,
    OpenRouterTTSSettings,
)
from app.video_generator import DEFAULT_VIDEO_MODEL, OpenRouterVideoClient


class RuntimeConfigTests(unittest.TestCase):
    def test_shared_root_key_and_production_defaults_configure_all_clients(self):
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "",
                "OPENROUTER_SCRIPT_API_KEY": "",
                "OPENROUTER_TTS_API_KEY": "",
                "OPENROUTER_VIDEO_API_KEY": "",
                "OPENROUTER_SCRIPT_MODEL": "",
                "OPENROUTER_TTS_MODEL": "",
                "OPENROUTER_TTS_VOICE": "",
                "OPENROUTER_VIDEO_MODEL": "",
            },
            clear=False,
        ), patch("app.core.config.settings.OPENROUTER_API_KEY", "shared-key"):
            script = OpenRouterClient.from_env()
            tts = OpenRouterTTSSettings.from_env()
            video = OpenRouterVideoClient.from_env()

        self.assertEqual(script.api_key, "shared-key")
        self.assertEqual(script.model, "openai/gpt-5.4-mini")
        self.assertEqual(tts.api_key, "shared-key")
        self.assertEqual(tts.model, DEFAULT_TTS_MODEL)
        self.assertEqual(tts.voice_name, DEFAULT_TTS_VOICE)
        self.assertEqual(video.api_key, "shared-key")
        self.assertEqual(video.model, DEFAULT_VIDEO_MODEL)

    @patch("app.runtime_config.get_video_model_capabilities")
    def test_resolves_script_duration_from_live_video_capabilities(self, get_capabilities):
        get_capabilities.return_value = VideoModelCapabilities(
            model_id="alibaba/wan-2.6",
            name="Alibaba: Wan 2.6",
            supported_durations=(5, 10),
            supported_aspect_ratios=("9:16",),
            supported_resolutions=("720p", "1080p"),
            generate_audio=True,
        )

        with patch.dict(os.environ, {"OPENROUTER_VIDEO_API_KEY": "video-key"}):
            duration, supported = resolve_script_generation_duration(15)

        self.assertEqual(duration, 10)
        self.assertEqual(supported, (5, 10))
        get_capabilities.assert_called_once_with(None)

    @patch("app.runtime_config.get_video_model_capabilities")
    def test_keeps_requested_duration_when_video_provider_is_not_configured(
        self, get_capabilities
    ):
        with patch.dict(os.environ, {}, clear=True), patch(
            "app.runtime_config.app_settings.OPENROUTER_API_KEY", ""
        ):
            duration, supported = resolve_script_generation_duration(15)

        self.assertEqual(duration, 15)
        self.assertIsNone(supported)
        get_capabilities.assert_not_called()


if __name__ == "__main__":
    unittest.main()
