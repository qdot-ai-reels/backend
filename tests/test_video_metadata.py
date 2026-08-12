import json
import unittest

from app.video_metadata import parse_ffprobe_output


class VideoMetadataTest(unittest.TestCase):
    def test_parses_video_stream_and_format_metadata(self):
        payload = {
            "streams": [{"codec_type": "video", "width": 720, "height": 1280}],
            "format": {"duration": "8.000000"},
        }

        metadata = parse_ffprobe_output(json.dumps(payload))

        self.assertEqual(metadata.width, 720)
        self.assertEqual(metadata.height, 1280)
        self.assertEqual(metadata.duration_seconds, 8.0)


if __name__ == "__main__":
    unittest.main()
