import unittest

from app.tts_generator import (
    GoogleTTSClient,
    NarrationValidationError,
    SceneAudioDurationError,
    SceneNarration,
    build_scene_narrations,
)


SCRIPT = {
    "scenes": [
        {"scene_number": 1, "time_range_sec": [0, 3], "voiceover": "첫 번째 장면입니다."},
        {"scene_number": 2, "time_range_sec": [3, 6], "voiceover": "두 번째 장면입니다."},
        {"scene_number": 3, "time_range_sec": [6, 8], "voiceover": "지금 확인해 보세요."},
    ]
}


class TTSGeneratorTests(unittest.TestCase):
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
                    {"scene_number": 1, "time_range_sec": [0, 3], "voiceover": None},
                    {"scene_number": 2, "time_range_sec": [3, 6], "voiceover": "다음 장면입니다."},
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
                        {"time_range_sec": [0, 3], "voiceover": "첫 장면"},
                        {"time_range_sec": [2, 5], "voiceover": "겹치는 장면"},
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

        client = GoogleTTSClient(
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
                {"scene_number": 1, "time_range_sec": [0, 3], "voiceover": None},
                {"scene_number": 2, "time_range_sec": [3, 6], "voiceover": "말하는 장면입니다."},
            ]
        }

        client = GoogleTTSClient(
            synthesizer=lambda text: synth_calls.append(text) or b"audio",
            combiner=lambda scenes: combine_calls.append(scenes) or b"combined",
            duration_reader=lambda _audio: 1.0,
        )

        self.assertEqual(client.generate_narration(script), b"combined")
        self.assertEqual(synth_calls, ["말하는 장면입니다."])
        self.assertIsNone(combine_calls[0][0].audio_content)
        self.assertEqual(combine_calls[0][0].end_seconds, 3.0)

    def test_rejects_scene_audio_longer_than_its_time_range(self):
        client = GoogleTTSClient(
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
        client = GoogleTTSClient(
            synthesizer=lambda text: calls.append(text) or b"audio",
            combiner=lambda _scenes: b"combined",
            duration_reader=lambda _audio: 0.5,
        )
        long_script = {
            "scenes": [
                {
                    "scene_number": 1,
                    "time_range_sec": [0, 1],
                    "voiceover": "이 문장은 일 초 안에 읽기에는 너무 긴 광고 내레이션입니다.",
                }
            ]
        }

        self.assertEqual(client.generate_narration(long_script), b"combined")
        self.assertEqual(calls, [long_script["scenes"][0]["voiceover"]])

if __name__ == "__main__":
    unittest.main()
