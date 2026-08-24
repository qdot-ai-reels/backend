import subprocess
import tempfile
import unittest
from pathlib import Path

from app.media_combiner import (
    MediaCombineError,
    combine_video_and_audio,
    read_media_duration,
)


class MediaCombinerTests(unittest.TestCase):
    def test_combines_video_and_audio_into_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            video_path = directory / "input.mp4"
            audio_path = directory / "narration.mp3"
            output_path = directory / "output.mp4"

            self._run_ffmpeg(
                [
                    "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video_path),
                ]
            )
            self._run_ffmpeg(
                [
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "libmp3lame", str(audio_path),
                ]
            )

            combine_video_and_audio(video_path, audio_path, output_path)

            self.assertTrue(output_path.exists())
            self.assertAlmostEqual(read_media_duration(output_path), 2.0, delta=0.15)
            self.assertEqual(self._stream_count(output_path, "v:0"), 1)
            self.assertEqual(self._stream_count(output_path, "a:0"), 1)

    def test_fails_when_video_and_audio_durations_do_not_match(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            video_path = directory / "input.mp4"
            audio_path = directory / "narration.mp3"
            output_path = directory / "output.mp4"

            self._run_ffmpeg([
                "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video_path),
            ])
            self._run_ffmpeg([
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:a", "libmp3lame", str(audio_path),
            ])

            with self.assertRaises(MediaCombineError) as context:
                combine_video_and_audio(video_path, audio_path, output_path)

            self.assertEqual(context.exception.error_type, "duration_mismatch")
            self.assertFalse(context.exception.retryable)
            self.assertEqual(context.exception.to_dict()["error_type"], "duration_mismatch")

    @staticmethod
    def _run_ffmpeg(arguments):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @staticmethod
    def _stream_count(path, selector):
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", selector,
                "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return len([line for line in result.stdout.splitlines() if line.strip()])


if __name__ == "__main__":
    unittest.main()
