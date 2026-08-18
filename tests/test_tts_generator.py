import json
import unittest
from unittest.mock import patch

from app.tts_generator import (
    OpenRouterTTSClient,
    NarrationValidationError,
    SceneAudioDurationError,
    SceneNarration,
    OpenRouterTTSClient,
    OpenRouterTTSSettings,
    build_scene_narrations,
)


SCRIPT = {
    "scenes": [
        {"time_range_sec": {"start": 0, "end": 3}, "auditory": {"voiceover": "첫 번째 장면입니다."}},
        {"time_range_sec": {"start": 3, "end": 6}, "auditory": {"voiceover": "두 번째 장면입니다."}},
        {"time_range_sec": {"start": 6, "end": 8}, "auditory": {"voiceover": "지금 확인해 보세요."}},
    ]
}


class TTSGeneratorTests(unittest.TestCase):
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

    def test_generates_audio_for_each_scene_and_combines_once(self):
        synth_calls = []
        combine_calls = []

        def fake_synthesizer(text):
            synth_calls.append(text)
            return f"audio:{text}".encode()

        def fake_combiner(scene_audio):
            combine_calls.append(scene_audio)
            return b"combined-mp3-bytes"

        client = OpenRouterTTSClient(
            synthesizer=fake_synthesizer,
            combiner=fake_combiner,
            duration_reader=lambda _audio: 1.5,
        )

        result = client.generate_narration(SCRIPT)

        self.assertEqual(result, b"combined-mp3-bytes")
        self.assertEqual(
            synth_calls,
            ["첫 번째 장면입니다.", "두 번째 장면입니다.", "지금 확인해 보세요."],
        )
        self.assertEqual(len(combine_calls), 1)
        self.assertEqual(
            combine_calls[0][0].audio_content,
            "audio:첫 번째 장면입니다.".encode(),
        )
        self.assertEqual(combine_calls[0][1].start_seconds, 3.0)

    def test_skips_tts_for_silent_scene_but_keeps_it_in_timeline(self):
        synth_calls = []
        combine_calls = []
        script = {
            "scenes": [
                {"time_range_sec": {"start": 0, "end": 3}, "auditory": {"voiceover": None}},
                {"time_range_sec": {"start": 3, "end": 6}, "auditory": {"voiceover": "말하는 장면입니다."}},
            ]
        }

        client = OpenRouterTTSClient(
            synthesizer=lambda text: synth_calls.append(text) or b"audio",
            combiner=lambda scenes: combine_calls.append(scenes) or b"combined",
            duration_reader=lambda _audio: 1.0,
        )

        self.assertEqual(client.generate_narration(script), b"combined")
        self.assertEqual(synth_calls, ["말하는 장면입니다."])
        self.assertIsNone(combine_calls[0][0].audio_content)
        self.assertEqual(combine_calls[0][0].end_seconds, 3.0)

    def test_supports_arbitrary_scene_durations_not_fixed_presets(self):
        synth_calls = []
        script = {
            "scenes": [
                {"time_range_sec": {"start": 0, "end": 2.5}, "auditory": {"voiceover": None}},
                {"time_range_sec": {"start": 2.5, "end": 5.75}, "auditory": {"voiceover": "두 번째 장면입니다."}},
                {"time_range_sec": {"start": 5.75, "end": 11.3}, "auditory": {"voiceover": "마지막 장면입니다."}},
            ]
        }

        client = OpenRouterTTSClient(
            synthesizer=lambda text: synth_calls.append(text) or b"audio",
            combiner=lambda _scenes: b"combined",
            duration_reader=lambda _audio: 1.0,
        )

        self.assertEqual(client.generate_narration(script), b"combined")
        self.assertEqual(synth_calls, ["두 번째 장면입니다.", "마지막 장면입니다."])

    def test_rejects_scene_audio_longer_than_its_time_range(self):
        client = OpenRouterTTSClient(
            synthesizer=lambda _text: b"audio",
            combiner=lambda _scenes: b"should-not-be-called",
            duration_reader=lambda _audio: 3.2,
        )

        with self.assertRaises(SceneAudioDurationError) as context:
            client.generate_narration(SCRIPT)

        self.assertEqual(context.exception.scene_number, 1)
        self.assertEqual(context.exception.expected_seconds, 3.0)
        self.assertEqual(context.exception.actual_seconds, 3.2)

    def test_does_not_duplicate_script_dialogue_validation_in_tts(self):
        calls = []
        client = OpenRouterTTSClient(
            synthesizer=lambda text: calls.append(text) or b"audio",
            combiner=lambda _scenes: b"combined",
            duration_reader=lambda _audio: 0.5,
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

if __name__ == "__main__":
    unittest.main()
