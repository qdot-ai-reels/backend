import unittest

from app.video_generation import (
    GenerationInput,
    GenerationStatus,
    RetryPolicy,
    generate_with_retry,
)
from app.video_validator import VideoMetadata, ValidationPolicy


class VideoGenerationTest(unittest.TestCase):
    def setUp(self):
        self.generation_input = GenerationInput(
            script={"scenes": [{"time_range_sec": {"start": 0, "end": 8}}]},
            image_reference="product-image.jpg",
        )
        self.validation_policy = ValidationPolicy(expected_duration_seconds=8.0)

    def test_returns_completed_when_first_video_is_valid(self):
        calls = []

        def generate_video(request, attempt):
            calls.append((request, attempt))
            return VideoMetadata(720, 1280, 8.0)

        result = generate_with_retry(
            self.generation_input,
            self.validation_policy,
            generate_video,
            RetryPolicy(max_retries=1),
        )

        self.assertEqual(result.status, GenerationStatus.COMPLETED)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(calls[0], (self.generation_input, 1))

    def test_regenerates_when_first_video_is_invalid(self):
        attempts = iter(
            [
                VideoMetadata(1280, 720, 8.0),
                VideoMetadata(720, 1280, 8.0),
            ]
        )
        received_inputs = []

        def generate_video(request, attempt):
            received_inputs.append(request)
            return next(attempts)

        result = generate_with_retry(
            self.generation_input,
            self.validation_policy,
            generate_video,
            RetryPolicy(max_retries=1),
        )

        self.assertEqual(result.status, GenerationStatus.COMPLETED)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.validation.errors, [])
        self.assertEqual(received_inputs, [self.generation_input] * 2)

    def test_returns_retry_exhausted_after_max_retries(self):
        calls = []

        def generate_video(request, attempt):
            calls.append(attempt)
            return VideoMetadata(1280, 720, 8.0)

        result = generate_with_retry(
            self.generation_input,
            self.validation_policy,
            generate_video,
            RetryPolicy(max_retries=2),
        )

        self.assertEqual(result.status, GenerationStatus.RETRY_EXHAUSTED)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(calls, [1, 2, 3])
        self.assertIn("aspect_ratio", result.validation.errors)


if __name__ == "__main__":
    unittest.main()
