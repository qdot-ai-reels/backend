"""Versioned, deterministic scene plans for Studio reel generation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class GenerationTemplateError(ValueError):
    """Raised when a template cannot be resolved or a script does not match it."""


@dataclass(frozen=True)
class TemplateScene:
    key: str
    label: str
    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def voiceover_max_syllables(self) -> int:
        # Keep this aligned with script_generator.DEFAULT_SYLLABLES_PER_SECOND
        # without importing that module and creating a circular dependency.
        return int(self.duration_seconds * 4.5)

    def to_public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "voiceover_max_syllables": self.voiceover_max_syllables,
        }


@dataclass(frozen=True)
class GenerationTemplate:
    template_id: str
    version: int
    name: str
    description: str
    duration_seconds: int
    scenes: tuple[TemplateScene, ...]

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.template_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "duration_seconds": self.duration_seconds,
            "scene_plan": [scene.to_public() for scene in self.scenes],
        }

    def prompt_scene_plan(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "key": scene.key,
                "label": scene.label,
                "start_seconds": scene.start_seconds,
                "end_seconds": scene.end_seconds,
            }
            for scene in self.scenes
        )


TEMPLATES: tuple[GenerationTemplate, ...] = (
    GenerationTemplate(
        template_id="ugc_quick_4",
        version=1,
        name="4초 초압축",
        description="상품을 즉시 보여주고 핵심 장점과 CTA만 빠르게 전달합니다.",
        duration_seconds=4,
        scenes=(
            TemplateScene("hook", "Hook", 0.0, 1.2),
            TemplateScene("product", "Product", 1.2, 2.8),
            TemplateScene("cta", "CTA", 2.8, 4.0),
        ),
    ),
    GenerationTemplate(
        template_id="ugc_quick_6",
        version=1,
        name="6초 빠른 소개",
        description="짧은 Hook 뒤 제품과 사용 장면, CTA를 한 번에 구성합니다.",
        duration_seconds=6,
        scenes=(
            TemplateScene("hook", "Hook", 0.0, 1.5),
            TemplateScene("product", "Product", 1.5, 3.5),
            TemplateScene("lifestyle", "Lifestyle", 3.5, 4.8),
            TemplateScene("cta", "CTA", 4.8, 6.0),
        ),
    ),
    GenerationTemplate(
        template_id="ugc_balanced_8",
        version=1,
        name="8초 균형형",
        description="Hook, 제품 설명, 사용 분위기와 CTA를 균형 있게 보여줍니다.",
        duration_seconds=8,
        scenes=(
            TemplateScene("hook", "Hook", 0.0, 2.0),
            TemplateScene("product", "Product", 2.0, 4.5),
            TemplateScene("lifestyle", "Lifestyle", 4.5, 6.5),
            TemplateScene("cta", "CTA", 6.5, 8.0),
        ),
    ),
    GenerationTemplate(
        template_id="ugc_full_15",
        version=1,
        name="15초 풀 스토리",
        description="모델과 상품 Hook부터 제품, 생활 장면, CTA까지 완결형으로 구성합니다.",
        duration_seconds=15,
        scenes=(
            TemplateScene("hook", "Hook", 0.0, 3.0),
            TemplateScene("product", "Product", 3.0, 8.0),
            TemplateScene("lifestyle", "Lifestyle", 8.0, 12.0),
            TemplateScene("cta", "CTA", 12.0, 15.0),
        ),
    ),
)

_TEMPLATE_BY_ID = {template.template_id: template for template in TEMPLATES}


def list_generation_templates() -> list[GenerationTemplate]:
    return list(TEMPLATES)


def get_generation_template(
    template_id: str,
    version: int | None = None,
) -> GenerationTemplate:
    template = _TEMPLATE_BY_ID.get(template_id)
    if template is None:
        raise GenerationTemplateError(f"지원하지 않는 영상 템플릿입니다: {template_id}")
    if version is not None and version != template.version:
        raise GenerationTemplateError(
            f"지원하지 않는 템플릿 버전입니다: {template_id} v{version}"
        )
    return template


def _scene_section(scene: Mapping[str, Any]) -> str | None:
    value = scene.get("section")
    if not isinstance(value, str):
        value = scene.get("scene_name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _assert_scene_shape(
    scenes: Any,
    plan: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    if not isinstance(scenes, list) or len(scenes) != len(plan):
        raise GenerationTemplateError(
            f"선택한 템플릿은 정확히 {len(plan)}개 scene이 필요합니다."
        )
    if not all(isinstance(scene, Mapping) for scene in scenes):
        raise GenerationTemplateError("템플릿 scene은 JSON 객체여야 합니다.")
    return scenes


def normalize_generated_script_to_plan(
    document: Mapping[str, Any],
    plan: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply the server-owned timeline to a newly generated script.

    The model owns each scene's creative fields, while the selected Studio
    template owns section names and timing. A different scene count is rejected
    rather than silently dropping or inventing creative content.
    """
    normalized = deepcopy(dict(document))
    scenes = _assert_scene_shape(normalized.get("scenes"), plan)
    for scene, expected in zip(scenes, plan, strict=True):
        label = str(expected["label"])
        if "section" in scene or "scene_name" not in scene:
            scene["section"] = label
        else:
            scene["scene_name"] = label
        scene["time_range_sec"] = {
            "start": expected["start_seconds"],
            "end": expected["end_seconds"],
        }
    duration = plan[-1]["end_seconds"]
    video = normalized.get("video")
    if isinstance(video, dict):
        video["video_duration"] = f"{duration:g}초"
    return normalized


def validate_script_matches_template(
    document: Mapping[str, Any],
    template: GenerationTemplate,
) -> dict[str, Any]:
    """Validate a user-supplied script against the exact selected template."""
    normalized = deepcopy(dict(document))
    scenes = _assert_scene_shape(normalized.get("scenes"), template.prompt_scene_plan())
    for index, (scene, expected) in enumerate(
        zip(scenes, template.scenes, strict=True), start=1
    ):
        section = _scene_section(scene)
        if section is None or section.casefold() != expected.label.casefold():
            raise GenerationTemplateError(
                f"{index}번째 scene section은 {expected.label}이어야 합니다."
            )
        time_range = scene.get("time_range_sec")
        if not isinstance(time_range, Mapping):
            raise GenerationTemplateError(
                f"{index}번째 scene의 time_range_sec가 필요합니다."
            )
        start = time_range.get("start")
        end = time_range.get("end")
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or abs(float(start) - expected.start_seconds) > 1e-6
            or abs(float(end) - expected.end_seconds) > 1e-6
        ):
            raise GenerationTemplateError(
                f"{index}번째 scene 시간은 "
                f"{expected.start_seconds:g}~{expected.end_seconds:g}초여야 합니다."
            )
        if "section" in scene or "scene_name" not in scene:
            scene["section"] = expected.label
        else:
            scene["scene_name"] = expected.label
        scene["time_range_sec"] = {
            "start": expected.start_seconds,
            "end": expected.end_seconds,
        }
    return normalized
