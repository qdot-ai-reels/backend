import json
import unittest
from unittest.mock import patch

from app.video_metadata import (
    center_crop_square_video_to_vertical,
    is_production_square_source,
    parse_ffprobe_output,
)
from app.video_validator import VideoMetadata


class VideoMetadataTest(unittest.TestCase):
    def test_parses_video_stream_and_format_metadata(self):
        payload = {
            "streams": [{
                "codec_type": "video",
                "width": 720,
                "height": 1280,
                "codec_name": "h264",
                "avg_frame_rate": "30000/1001",
                "bit_rate": "5000000",
            }],
            "format": {"duration": "8.000000"},
        }

        metadata = parse_ffprobe_output(json.dumps(payload))

        self.assertEqual(metadata.width, 720)
        self.assertEqual(metadata.height, 1280)
        self.assertEqual(metadata.duration_seconds, 8.0)
        self.assertAlmostEqual(metadata.fps, 29.97, places=2)
        self.assertEqual(metadata.codec, "h264")
        self.assertEqual(metadata.bitrate, 5_000_000)

    def test_square_source_eligibility_is_strict(self):
        self.assertTrue(
            is_production_square_source(VideoMetadata(1440, 1440, 4.0))
        )
        self.assertTrue(
            is_production_square_source(VideoMetadata(1080, 1080, 4.0))
        )
        self.assertTrue(
            is_production_square_source(VideoMetadata(1440, 1400, 4.0))
        )
        self.assertTrue(
            is_production_square_source(VideoMetadata(1400, 1440, 4.0))
        )
        self.assertTrue(
            is_production_square_source(VideoMetadata(1440, 1368, 4.0))
        )
        self.assertFalse(
            is_production_square_source(VideoMetadata(1440, 1367, 4.0))
        )
        self.assertFalse(
            is_production_square_source(VideoMetadata(1079, 1079, 4.0))
        )
        self.assertFalse(
            is_production_square_source(VideoMetadata(1920, 1080, 4.0))
        )

    @patch("app.video_metadata.subprocess.run")
    def test_center_crop_helper_uses_production_ffmpeg_contract(self, run):
        center_crop_square_video_to_vertical(
            "provider.mp4",
            "vertical.mp4",
            VideoMetadata(1440, 1440, 4.0),
        )

        command = run.call_args.args[0]
        filter_graph = command[command.index("-vf") + 1]
        self.assertIn("crop='trunc(ih*9/16/2)*2':ih:'(iw-ow)/2':0", filter_graph)
        self.assertIn("scale=1080:1920:flags=lanczos", filter_graph)
        self.assertIn("setsar=1", filter_graph)
        self.assertIn("format=yuv420p", filter_graph)
        self.assertIn("-an", command)
        self.assertEqual(command[command.index("-r") + 1], "30")
        self.assertEqual(command[command.index("-preset") + 1], "slow")
        self.assertEqual(command[command.index("-b:v") + 1], "8M")
        self.assertEqual(command[command.index("-maxrate") + 1], "10M")
        self.assertEqual(command[command.index("-bufsize") + 1], "16M")
        self.assertEqual(command[command.index("-movflags") + 1], "+faststart")

    @patch("app.video_metadata.subprocess.run")
    def test_center_crop_helper_rejects_low_resolution_without_ffmpeg(self, run):
        with self.assertRaisesRegex(ValueError, "최소 1080x1080"):
            center_crop_square_video_to_vertical(
                "provider.mp4",
                "vertical.mp4",
                VideoMetadata(1079, 1079, 4.0),
            )

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
