import os
import unittest
from unittest.mock import patch

from app.runtime_config import resolve_script_generation_duration
from app.settings_service import VideoModelCapabilities


class RuntimeConfigTests(unittest.TestCase):
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
        with patch.dict(os.environ, {}, clear=True):
            duration, supported = resolve_script_generation_duration(15)

        self.assertEqual(duration, 15)
        self.assertIsNone(supported)
        get_capabilities.assert_not_called()


if __name__ == "__main__":
    unittest.main()
