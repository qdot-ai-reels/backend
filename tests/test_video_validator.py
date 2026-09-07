import unittest

from app.video_validator import VideoMetadata, ValidationPolicy, validate_video


class VideoValidatorTest(unittest.TestCase):
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
