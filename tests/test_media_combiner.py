import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.media_combiner import (
    MediaCombineError,
    combine_video_and_audio,
    remove_audio_track,
    read_media_duration,
)


class MediaCombinerTests(unittest.TestCase):
    @patch("app.media_combiner.subprocess.run")
    @patch("app.media_combiner.read_media_duration", side_effect=[2.0, 2.0])
    def test_merge_uses_production_loudness_without_reencoding_video(
        self,
        _read_duration,
        run,
    ):
        run.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr=(
                    "measurement output\n"
                    '{"input_i":"-31.31","input_tp":"-12.25",'
                    '"input_lra":"1.20","input_thresh":"-41.40",'
                    '"target_offset":"0.05"}'
                ),
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            video_path = directory / "input.mp4"
            audio_path = directory / "narration.mp3"
            output_path = directory / "output.mp4"
            video_path.write_bytes(b"video")
            audio_path.write_bytes(b"audio")

            combine_video_and_audio(video_path, audio_path, output_path)

        self.assertEqual(run.call_count, 2)
        measurement_command = run.call_args_list[0].args[0]
        self.assertEqual(
            measurement_command[measurement_command.index("-af") + 1],
            "aformat=channel_layouts=stereo,"
            "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
        )
        self.assertIn("-nostdin", measurement_command)
        self.assertEqual(
            measurement_command[measurement_command.index("-map") + 1],
            "0:a:0",
        )
        self.assertIn("-vn", measurement_command)
        command = run.call_args_list[1].args[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(
            filter_graph,
            "[1:a]aformat=channel_layouts=stereo,"
            "loudnorm=I=-16:TP=-1.5:LRA=11:"
            "measured_I=-31.31:measured_TP=-12.25:measured_LRA=1.2:"
            "measured_thresh=-41.4:offset=0.05:linear=true[audio]",
        )
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-ar") + 1], "48000")
        self.assertEqual(command[command.index("-ac") + 1], "2")
        self.assertEqual(command[command.index("-movflags") + 1], "+faststart")

    @patch("app.media_combiner.subprocess.run")
    @patch("app.media_combiner.read_media_duration", side_effect=[2.0, 2.0])
    def test_merge_falls_back_safely_when_loudness_measurement_is_invalid(
        self,
        _read_duration,
        run,
    ):
        run.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr=(
                    '{"input_i":"-inf","input_tp":"-12.25",'
                    '"input_lra":"1.20","input_thresh":"-41.40",'
                    '"target_offset":"0.05"}'
                ),
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            video_path = directory / "input.mp4"
            audio_path = directory / "narration.mp3"
            output_path = directory / "output.mp4"
            video_path.write_bytes(b"video")
            audio_path.write_bytes(b"audio")

            combine_video_and_audio(video_path, audio_path, output_path)

        command = run.call_args_list[1].args[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(
            filter_graph,
            "[1:a]aformat=channel_layouts=stereo,"
            "loudnorm=I=-16:TP=-1.5:LRA=11[audio]",
        )

    def test_removes_provider_audio_from_video(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            video_path = directory / "input-with-audio.mp4"
            output_path = directory / "video-only.mp4"

            self._run_ffmpeg([
                "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(video_path),
            ])

            remove_audio_track(video_path, output_path)

            self.assertEqual(self._stream_count(output_path, "v:0"), 1)
            self.assertEqual(self._stream_count(output_path, "a:0"), 0)

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
            self.assertEqual(self._audio_channels(output_path), 2)

    def test_rejects_video_and_audio_with_different_durations(self):
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
            self.assertFalse(output_path.exists())

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

    @staticmethod
    def _audio_channels(path):
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=channels", "-of",
                "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return int(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
