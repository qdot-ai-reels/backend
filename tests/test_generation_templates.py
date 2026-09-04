import unittest

from app.generation_templates import (
    GenerationTemplateError,
    get_generation_template,
    list_generation_templates,
    normalize_generated_script_to_plan,
    validate_script_matches_template,
)


class GenerationTemplateTests(unittest.TestCase):
    def test_exposes_the_four_versioned_exact_scene_plans(self):
        plans = {
            template.template_id: [
                (scene.label, scene.start_seconds, scene.end_seconds)
                for scene in template.scenes
            ]
            for template in list_generation_templates()
        }

        self.assertEqual(
            plans,
            {
                "ugc_quick_4": [
                    ("Hook", 0.0, 1.2),
                    ("Product", 1.2, 2.8),
                    ("CTA", 2.8, 4.0),
                ],
                "ugc_quick_6": [
                    ("Hook", 0.0, 1.5),
                    ("Product", 1.5, 3.5),
                    ("Lifestyle", 3.5, 4.8),
                    ("CTA", 4.8, 6.0),
                ],
                "ugc_balanced_8": [
                    ("Hook", 0.0, 2.0),
                    ("Product", 2.0, 4.5),
                    ("Lifestyle", 4.5, 6.5),
                    ("CTA", 6.5, 8.0),
                ],
                "ugc_full_15": [
                    ("Hook", 0.0, 3.0),
                    ("Product", 3.0, 8.0),
                    ("Lifestyle", 8.0, 12.0),
                    ("CTA", 12.0, 15.0),
                ],
            },
        )
        self.assertTrue(all(template.version == 1 for template in list_generation_templates()))

    def test_rejects_an_unknown_version(self):
        with self.assertRaisesRegex(GenerationTemplateError, "버전"):
            get_generation_template("ugc_full_15", 2)

    def test_normalizes_model_generated_timeline_to_server_plan(self):
        template = get_generation_template("ugc_full_15")
        script = {
            "video": {"video_duration": "14초"},
            "scenes": [
                {"section": "anything", "time_range_sec": {"start": 0, "end": 1}}
                for _ in range(4)
            ],
        }

        result = normalize_generated_script_to_plan(script, template.prompt_scene_plan())

        self.assertEqual(
            [scene["section"] for scene in result["scenes"]],
            ["Hook", "Product", "Lifestyle", "CTA"],
        )
        self.assertEqual(result["scenes"][3]["time_range_sec"], {"start": 12.0, "end": 15.0})
        self.assertEqual(result["video"]["video_duration"], "15초")

    def test_rejects_user_script_with_non_matching_timeline(self):
        template = get_generation_template("ugc_full_15")
        script = {
            "scenes": [
                {
                    "section": scene.label,
                    "time_range_sec": {
                        "start": scene.start_seconds,
                        "end": scene.end_seconds,
                    },
                }
                for scene in template.scenes
            ]
        }
        script["scenes"][3]["time_range_sec"]["start"] = 11

        with self.assertRaisesRegex(GenerationTemplateError, "12~15초"):
            validate_script_matches_template(script, template)


if __name__ == "__main__":
    unittest.main()
