import unittest

from app.video_validator import VideoMetadata, ValidationPolicy, validate_video


class VideoValidatorTest(unittest.TestCase):
    def test_production_policy_requires_full_hd_fps_codec_bitrate_and_black_frames(self):
        policy = ValidationPolicy.production(expected_duration_seconds=8.0)
        result = validate_video(
            VideoMetadata(
                width=1080,
                height=1920,
                duration_seconds=8.1,
                fps=30.0,
                codec="h264",
                bitrate=10_000_000,
                black_frame_ratio=0.0,
            ),
            policy,
        )

        self.assertTrue(result.is_valid)
        self.assertTrue(all(item["passed"] for item in result.checks.values()))

    def test_production_policy_rejects_low_resolution_and_missing_technical_metadata(self):
        result = validate_video(
            VideoMetadata(width=720, height=1280, duration_seconds=8.0),
            ValidationPolicy.production(expected_duration_seconds=8.0),
        )

        self.assertFalse(result.is_valid)
        self.assertTrue(
            {"resolution", "fps", "codec", "bitrate", "black_frames"}.issubset(
                result.errors
            )
        )

    def test_accepts_video_matching_policy(self):
        result = validate_video(
            VideoMetadata(width=720, height=1280, duration_seconds=8.0),
            ValidationPolicy(expected_duration_seconds=8.0),
        )

        self.assertTrue(result.is_valid)
        self.assertEqual(result.errors, [])

    def test_rejects_video_with_wrong_aspect_ratio(self):
        result = validate_video(
            VideoMetadata(width=1280, height=720, duration_seconds=8.0),
            ValidationPolicy(expected_duration_seconds=8.0),
        )

        self.assertFalse(result.is_valid)
        self.assertIn("aspect_ratio", result.errors)

    def test_rejects_video_above_maximum_resolution(self):
        result = validate_video(
            VideoMetadata(width=1440, height=2560, duration_seconds=8.0),
            ValidationPolicy(expected_duration_seconds=8.0),
        )

        self.assertFalse(result.is_valid)
        self.assertIn("resolution", result.errors)

    def test_rejects_video_with_wrong_duration(self):
        result = validate_video(
            VideoMetadata(width=720, height=1280, duration_seconds=6.0),
            ValidationPolicy(expected_duration_seconds=8.0),
        )

        self.assertFalse(result.is_valid)
        self.assertIn("duration", result.errors)


if __name__ == "__main__":
    unittest.main()
