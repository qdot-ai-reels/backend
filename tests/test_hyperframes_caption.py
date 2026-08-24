import unittest

from app.hyperframes_caption import build_composition_html, build_transcript


class HyperFramesCaptionTests(unittest.TestCase):
    def test_build_transcript_maps_scene_subtitles_to_caption_cues(self) -> None:
        script = {
            "meta": {
                "output_format_version": "1.0",
                "framework": "Hook-Body-CTA",
                "language": "ko",
            },
            "summary": {
                "main_target": "보호자",
                "pain_point": "제품 선택이 어려움",
                "product_usp": "핵심 장점",
                "key_message": "상품 소개",
                "tone_and_manner": "생활형 광고",
            },
            "scenes": [
                {
                    "scene_name": "Hook",
                    "time_range_sec": {"start": 0, "end": 3},
                    "visual": "상품을 보여준다.",
                    "auditory": {"subtitle": "첫 장면", "voiceover": "첫 장면"},
                    "notes": "후킹",
                },
                {
                    "scene_name": "CTA",
                    "time_range_sec": {"start": 3, "end": 8},
                    "visual": "상품을 강조한다.",
                    "auditory": {"subtitle": "마지막 장면", "voiceover": "마지막 장면"},
                    "notes": "구매 유도",
                },
            ],
            "compliance_notes": {"avoid": [], "focus": []},
        }

        self.assertEqual(
            build_transcript(script),
            [
                {"id": "w0", "text": "첫 장면", "start": 0.0, "end": 3.0},
                {"id": "w1", "text": "마지막 장면", "start": 3.0, "end": 8.0},
            ],
        )

    def test_build_transcript_rejects_invalid_script(self) -> None:
        with self.assertRaises(ValueError):
            build_transcript({"scenes": []})

    def test_build_transcript_skips_empty_and_null_subtitles(self) -> None:
        script = {
            "meta": {
                "output_format_version": "1.0",
                "framework": "Hook-Body-CTA",
                "language": "ko",
            },
            "summary": {
                "main_target": "보호자",
                "pain_point": "제품 선택이 어려움",
                "product_usp": "핵심 장점",
                "key_message": "상품 소개",
                "tone_and_manner": "생활형 광고",
            },
            "scenes": [
                {
                    "scene_name": "Hook",
                    "time_range_sec": {"start": 0, "end": 2},
                    "visual": "상품을 보여준다.",
                    "auditory": {"subtitle": None, "voiceover": None},
                    "notes": "자막 없음",
                },
                {
                    "scene_name": "Body",
                    "time_range_sec": {"start": 2, "end": 5},
                    "visual": "제품을 사용한다.",
                    "auditory": {"subtitle": "   ", "voiceover": None},
                    "notes": "빈 자막",
                },
                {
                    "scene_name": "CTA",
                    "time_range_sec": {"start": 5, "end": 8},
                    "visual": "제품을 강조한다.",
                    "auditory": {"subtitle": "지금 확인", "voiceover": "지금 확인"},
                    "notes": "구매 유도",
                },
            ],
            "compliance_notes": {"avoid": [], "focus": []},
        }

        self.assertEqual(
            build_transcript(script),
            [{"id": "w0", "text": "지금 확인", "start": 5.0, "end": 8.0}],
        )

    def test_build_composition_html_uses_transcript_cue_boundaries(self) -> None:
        html = build_composition_html(
            video_filename="combined.mp4",
            transcript=[
                {"id": "w0", "text": "첫 장면 <확인>", "start": 0.0, "end": 3.0},
                {"id": "w1", "text": "마지막 장면", "start": 3.0, "end": 8.0},
            ],
            width=1080,
            height=1920,
        )

        self.assertIn('src="combined.mp4"', html)
        self.assertIn('data-width="1080"', html)
        self.assertIn('data-height="1920"', html)
        self.assertIn('data-fps="30"', html)
        self.assertIn('data-duration="8.0"', html)
        self.assertIn('data-start="0.0" data-duration="3.0"', html)
        self.assertIn("첫 장면 &lt;확인&gt;", html)
        self.assertNotIn("첫 장면 <확인>", html)

    def test_build_composition_html_allows_composition_without_captions(self) -> None:
        html = build_composition_html(
            video_filename="combined.mp4",
            transcript=[],
            duration_seconds=8,
        )

        self.assertIn('data-duration="8.0"', html)
        self.assertNotIn('class="caption clip"', html)


if __name__ == "__main__":
    unittest.main()
