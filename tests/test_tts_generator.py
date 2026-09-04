import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.tts_generator import (
    OpenRouterTTSClient,
    NarrationValidationError,
    CombinedAudioDurationError,
    SceneAudioDurationError,
    SceneNarration,
    OpenRouterTTSSettings,
    build_scene_narrations,
    stretch_audio_to_duration,
)


SCRIPT = {
    "scenes": [
        {"time_range_sec": {"start": 0, "end": 3}, "auditory": {"voiceover": "첫 번째 장면입니다."}},
        {"time_range_sec": {"start": 3, "end": 6}, "auditory": {"voiceover": "두 번째 장면입니다."}},
        {"time_range_sec": {"start": 6, "end": 8}, "auditory": {"voiceover": "지금 확인해 보세요."}},
    ]
}


class TTSGeneratorTests(unittest.TestCase):
    @patch("app.tts_generator.convert_pcm_to_mp3", return_value=b"converted-mp3")
    @patch("app.tts_generator.urlopen")
    def test_gemini_requests_pcm_and_converts_it_to_mp3(self, urlopen, convert_pcm):
        response = type(
            "Response",
            (),
            {
                "__enter__": lambda self: self,
                "__exit__": lambda self, *_args: None,
                "read": lambda self: b"raw-pcm",
            },
        )()
        urlopen.return_value = response
        client = OpenRouterTTSClient(
            settings=OpenRouterTTSSettings(
                api_key="test-key",
                model="google/gemini-3.1-flash-tts-preview",
                voice_name="Aoede",
            )
        )

        self.assertEqual(client.synthesizer("안녕하세요."), b"converted-mp3")
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload["response_format"], "pcm")
        self.assertEqual(payload["voice"], "Aoede")
        convert_pcm.assert_called_once_with(b"raw-pcm")

    @patch("app.tts_generator.urlopen")
    def test_omits_voice_when_not_configured(self, urlopen):
        response = type(
            "Response",
            (),
            {
                "__enter__": lambda self: self,
                "__exit__": lambda self, *_args: None,
                "read": lambda self: b"mp3-bytes",
            },
        )()
        urlopen.return_value = response

        client = OpenRouterTTSClient(
            settings=OpenRouterTTSSettings(
                api_key="test-key",
                model="fish-audio/s2.1-pro-free:free",
                voice_name="",
            )
        )

        self.assertEqual(client.synthesizer("안녕하세요."), b"mp3-bytes")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertNotIn("voice", payload)
        self.assertEqual(payload["model"], "fish-audio/s2.1-pro-free:free")

    @patch("app.tts_generator.urlopen")
    def test_includes_configured_voice(self, urlopen):
        response = type(
            "Response",
            (),
            {
                "__enter__": lambda self: self,
                "__exit__": lambda self, *_args: None,
                "read": lambda self: b"mp3-bytes",
            },
        )()
        urlopen.return_value = response

        client = OpenRouterTTSClient(
            settings=OpenRouterTTSSettings(
                api_key="test-key",
                model="test-model",
                voice_name="test-voice",
            )
        )

        client.synthesizer("테스트")
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload["voice"], "test-voice")

    def test_builds_scene_narrations_with_time_ranges(self):
        self.assertEqual(
            build_scene_narrations(SCRIPT),
            [
                SceneNarration(1, 0.0, 3.0, "첫 번째 장면입니다."),
                SceneNarration(2, 3.0, 6.0, "두 번째 장면입니다."),
                SceneNarration(3, 6.0, 8.0, "지금 확인해 보세요."),
            ],
        )

    def test_treats_missing_voiceover_as_silence(self):
        narrations = build_scene_narrations(
            {
                "scenes": [
                    {"time_range_sec": {"start": 0, "end": 3}, "auditory": {"voiceover": None}},
                    {"time_range_sec": {"start": 3, "end": 6}, "auditory": {"voiceover": "다음 장면입니다."}},
                ]
            }
        )

        self.assertEqual(narrations[0].text, "")
        self.assertIsNone(narrations[0].audio_content)
        self.assertEqual(narrations[1].text, "다음 장면입니다.")

    def test_rejects_overlapping_scene_time_ranges(self):
        with self.assertRaises(NarrationValidationError):
            build_scene_narrations(
                {
                    "scenes": [
                        {"time_range_sec": {"start": 0, "end": 3}, "auditory": {"voiceover": "첫 장면"}},
                        {"time_range_sec": {"start": 2, "end": 5}, "auditory": {"voiceover": "겹치는 장면"}},
                    ]
                }
            )

    def test_generates_one_continuous_performance_and_combines_once(self):
        synth_calls = []
        combine_calls = []

        def fake_synthesizer(text):
            synth_calls.append(text)
            return f"audio:{text}".encode()

        def fake_combiner(scene_audio):
            combine_calls.append(scene_audio)
            return b"combined-mp3-bytes"

        durations = iter([7.95, 8.0])
        client = OpenRouterTTSClient(
            synthesizer=fake_synthesizer,
            combiner=fake_combiner,
            duration_reader=lambda _audio: next(durations),
            time_fitter=lambda *_args: self.fail(
                "already fitted narration must not be stretched"
            ),
        )

        result = client.generate_narration(SCRIPT)

        self.assertEqual(result, b"combined-mp3-bytes")
        self.assertEqual(
            synth_calls,
            ["첫 번째 장면입니다. 두 번째 장면입니다. 지금 확인해 보세요."],
        )
        self.assertEqual(len(combine_calls), 1)
        self.assertEqual(
            combine_calls[0][0].audio_content,
            "audio:첫 번째 장면입니다. 두 번째 장면입니다. 지금 확인해 보세요.".encode(),
        )
        self.assertIsNone(combine_calls[0][1].audio_content)

    def test_time_fits_underfilled_continuous_audio_in_one_provider_call(self):
        script = {
            "scenes": [
                {
                    "time_range_sec": {"start": 0, "end": 3},
                    "auditory": {"voiceover": "상품을 소개합니다."},
                },
                {
                    "time_range_sec": {"start": 3, "end": 8},
                    "auditory": {"voiceover": "핵심 기능을 보여드립니다."},
                },
                {
                    "time_range_sec": {"start": 8, "end": 12},
                    "auditory": {"voiceover": "사용 모습을 확인하세요."},
                },
                {
                    "time_range_sec": {"start": 12, "end": 15},
                    "auditory": {"voiceover": "지금 시작하세요."},
                },
            ]
        }
        synth_calls = []
        fit_calls = []
        combine_calls = []
        durations = iter([11.9, 15.0])

        def fit_audio(audio, source_seconds, target_seconds):
            fit_calls.append((audio, source_seconds, target_seconds))
            return b"time-fitted-audio"

        client = OpenRouterTTSClient(
            synthesizer=lambda text: synth_calls.append(text) or b"raw-audio",
            time_fitter=fit_audio,
            combiner=lambda scenes: combine_calls.append(scenes) or b"combined",
            duration_reader=lambda _audio: next(durations),
        )

        self.assertEqual(client.generate_narration(script), b"combined")
        self.assertEqual(len(synth_calls), 1)
        self.assertEqual(fit_calls, [(b"raw-audio", 11.9, 15.0)])
        self.assertEqual(combine_calls[0][0].audio_content, b"time-fitted-audio")
        self.assertTrue(
            all(scene.audio_content is None for scene in combine_calls[0][1:])
        )

    def test_caps_time_fit_instead_of_extremely_stretching_short_audio(self):
        fit_calls = []
        durations = iter([5.0, 15.0])
        client = OpenRouterTTSClient(
            synthesizer=lambda _text: b"raw-audio",
            time_fitter=lambda audio, source, target: (
                fit_calls.append((audio, source, target)) or b"bounded-fit"
            ),
            combiner=lambda _scenes: b"combined",
            duration_reader=lambda _audio: next(durations),
        )
        script = {
            "scenes": [
                {
                    "time_range_sec": {"start": 0, "end": 15},
                    "auditory": {"voiceover": "짧은 문장"},
                },
            ]
        }

        self.assertEqual(client.generate_narration(script), b"combined")
        self.assertEqual(fit_calls, [(b"raw-audio", 5.0, 6.5)])

    def test_time_fit_targets_only_the_voiced_timeline_window(self):
        fit_calls = []
        durations = iter([5.0, 15.0])
        script = {
            "scenes": [
                {
                    "time_range_sec": {"start": 0, "end": 6},
                    "auditory": {"voiceover": "말하는 장면입니다."},
                },
                {
                    "time_range_sec": {"start": 6, "end": 15},
                    "auditory": {"voiceover": None},
                },
            ]
        }
        client = OpenRouterTTSClient(
            synthesizer=lambda _text: b"raw-audio",
            time_fitter=lambda audio, source, target: (
                fit_calls.append((audio, source, target)) or b"fitted-audio"
            ),
            combiner=lambda _scenes: b"combined",
            duration_reader=lambda _audio: next(durations),
        )

        self.assertEqual(client.generate_narration(script), b"combined")
        self.assertEqual(fit_calls, [(b"raw-audio", 5.0, 6.0)])

    @patch("app.tts_generator.subprocess.run")
    def test_time_fitter_uses_atempo_and_emits_the_requested_duration(self, run):
        def create_output(command, **_kwargs):
            Path(command[-1]).write_bytes(b"fitted-mp3")

        run.side_effect = create_output

        self.assertEqual(
            stretch_audio_to_duration(b"source-mp3", 8.0, 10.0),
            b"fitted-mp3",
        )

        command = run.call_args.args[0]
        audio_filter = command[command.index("-filter:a") + 1]
        self.assertIn("atempo=0.80000000", audio_filter)
        self.assertIn("atrim=duration=10.000", audio_filter)

    def test_skips_tts_for_silent_scene_but_keeps_it_in_timeline(self):
        synth_calls = []
        combine_calls = []
        script = {
            "scenes": [
                {"time_range_sec": {"start": 0, "end": 3}, "auditory": {"voiceover": None}},
                {"time_range_sec": {"start": 3, "end": 6}, "auditory": {"voiceover": "말하는 장면입니다."}},
            ]
        }

        durations = iter([3.0, 6.0])
        client = OpenRouterTTSClient(
            synthesizer=lambda text: synth_calls.append(text) or b"audio",
            combiner=lambda scenes: combine_calls.append(scenes) or b"combined",
            duration_reader=lambda _audio: next(durations),
        )

        self.assertEqual(client.generate_narration(script), b"combined")
        self.assertEqual(synth_calls, ["말하는 장면입니다."])
        self.assertIsNone(combine_calls[0][0].audio_content)
        self.assertEqual(combine_calls[0][0].end_seconds, 3.0)
        self.assertEqual(combine_calls[0][1].start_seconds, 3.0)
        self.assertEqual(combine_calls[0][1].audio_content, b"audio")

    def test_all_silent_scenes_preserve_full_timeline_without_synthesis(self):
        synth_calls = []
        fit_calls = []
        combine_calls = []
        script = {
            "scenes": [
                {"time_range_sec": {"start": 0, "end": 2}, "auditory": {"voiceover": None}},
                {"time_range_sec": {"start": 2, "end": 5}, "auditory": {"voiceover": ""}},
            ]
        }
        client = OpenRouterTTSClient(
            synthesizer=lambda text: synth_calls.append(text) or b"audio",
            time_fitter=lambda *args: fit_calls.append(args) or b"fitted",
            combiner=lambda scenes: combine_calls.append(scenes) or b"silence",
            duration_reader=lambda _audio: 5.0,
        )

        self.assertEqual(client.generate_narration(script), b"silence")
        self.assertEqual(synth_calls, [])
        self.assertEqual(fit_calls, [])
        self.assertEqual(len(combine_calls[0]), 2)
        self.assertTrue(all(scene.audio_content is None for scene in combine_calls[0]))

    def test_supports_arbitrary_scene_durations_not_fixed_presets(self):
        synth_calls = []
        script = {
            "scenes": [
                {"time_range_sec": {"start": 0, "end": 2.5}, "auditory": {"voiceover": None}},
                {"time_range_sec": {"start": 2.5, "end": 5.75}, "auditory": {"voiceover": "두 번째 장면입니다."}},
                {"time_range_sec": {"start": 5.75, "end": 11.3}, "auditory": {"voiceover": "마지막 장면입니다."}},
            ]
        }

        durations = iter([8.8, 11.3])
        client = OpenRouterTTSClient(
            synthesizer=lambda text: synth_calls.append(text) or b"audio",
            combiner=lambda _scenes: b"combined",
            duration_reader=lambda _audio: next(durations),
        )

        self.assertEqual(client.generate_narration(script), b"combined")
        self.assertEqual(synth_calls, ["두 번째 장면입니다. 마지막 장면입니다."])

    def test_rejects_continuous_audio_longer_than_remaining_timeline(self):
        client = OpenRouterTTSClient(
            synthesizer=lambda _text: b"audio",
            combiner=lambda _scenes: b"should-not-be-called",
            duration_reader=lambda _audio: 8.2,
            time_fitter=lambda *_args: self.fail(
                "overlong narration must fail before time fitting"
            ),
        )

        with self.assertRaises(SceneAudioDurationError) as context:
            client.generate_narration(SCRIPT)

        self.assertEqual(context.exception.scene_number, 3)
        self.assertEqual(context.exception.expected_seconds, 8.0)
        self.assertEqual(context.exception.actual_seconds, 8.2)

    def test_retries_tts_generation_up_to_five_times(self):
        attempts = []
        durations = iter([8.2, 7.5, 8.0])

        def synthesize(text):
            attempts.append(text)
            return b"audio"

        client = OpenRouterTTSClient(
            synthesizer=synthesize,
            combiner=lambda _scenes: b"combined",
            duration_reader=lambda _audio: next(durations),
            time_fitter=lambda _audio, _source, _target: b"fitted",
        )

        self.assertEqual(client.generate_narration(SCRIPT), b"combined")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(client.max_retries, 5)

    def test_raises_after_five_tts_retries_are_exhausted(self):
        calls = []
        client = OpenRouterTTSClient(
            synthesizer=lambda text: calls.append(text) or b"audio",
            combiner=lambda _scenes: b"should-not-be-called",
            duration_reader=lambda _audio: 8.2,
        )

        with self.assertRaises(SceneAudioDurationError):
            client.generate_narration(SCRIPT)

        self.assertEqual(len(calls), 6)

    def test_does_not_duplicate_script_dialogue_validation_in_tts(self):
        calls = []
        durations = iter([0.5, 1.0])
        client = OpenRouterTTSClient(
            synthesizer=lambda text: calls.append(text) or b"audio",
            combiner=lambda _scenes: b"combined",
            duration_reader=lambda _audio: next(durations),
            time_fitter=lambda audio, _source, _target: audio,
        )
        long_script = {
            "scenes": [
                {
                    "time_range_sec": {"start": 0, "end": 1},
                    "auditory": {"voiceover": "이 문장은 일 초 안에 읽기에는 너무 긴 광고 내레이션입니다."},
                }
            ]
        }

        self.assertEqual(client.generate_narration(long_script), b"combined")
        self.assertEqual(calls, [long_script["scenes"][0]["auditory"]["voiceover"]])

    def test_rejects_combined_audio_when_total_duration_does_not_match_script(self):
        durations = iter([7.0, 8.2])
        client = OpenRouterTTSClient(
            synthesizer=lambda _text: b"scene-audio",
            combiner=lambda _scenes: b"combined-audio",
            duration_reader=lambda _audio: next(durations),
            time_fitter=lambda audio, _source, _target: audio,
            max_retries=0,
        )

        with self.assertRaises(CombinedAudioDurationError) as context:
            client.generate_narration(SCRIPT)

        self.assertEqual(context.exception.expected_seconds, 8.0)
        self.assertEqual(context.exception.actual_seconds, 8.2)

if __name__ == "__main__":
    unittest.main()
