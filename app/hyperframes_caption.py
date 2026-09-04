"""Convert the PRD script format into HyperFrames caption cues."""

from __future__ import annotations

from html import escape
from typing import Any, Mapping

from app.script_generator import (
    ScriptValidationError,
    normalize_script_subtitles,
    validate_script_document,
)


def build_transcript(script: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build scene-level transcript cues from the validated script document."""
    try:
        validated_script = validate_script_document(normalize_script_subtitles(script))
    except ScriptValidationError as error:
        raise ValueError(f"HyperFrames transcript 입력이 올바르지 않습니다: {error}") from error

    transcript: list[dict[str, Any]] = []
    for scene in validated_script["scenes"]:
        time_range = scene["time_range_sec"]
        subtitle = scene["auditory"]["subtitle"]
        if not isinstance(subtitle, str) or not subtitle.strip():
            continue
        transcript.append(
            {
                "id": f"w{len(transcript)}",
                "text": subtitle,
                "start": float(time_range["start"]),
                "end": float(time_range["end"]),
            }
        )

    return transcript


def build_composition_html(
    video_filename: str,
    transcript: list[Mapping[str, Any]],
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    duration_seconds: float | None = None,
) -> str:
    """Build a basic HyperFrames composition with optional scene captions."""
    if not video_filename.strip():
        raise ValueError("HyperFrames composition 영상 파일명이 필요합니다.")
    if width < 1 or height < 1 or fps < 1:
        raise ValueError("HyperFrames composition의 해상도와 fps는 양수여야 합니다.")
    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("HyperFrames composition의 영상 길이는 양수여야 합니다.")

    cues: list[str] = []
    animations: list[str] = []
    duration = float(duration_seconds or 0)
    for index, cue in enumerate(transcript):
        text = cue.get("text")
        start = cue.get("start")
        end = cue.get("end")
        if not isinstance(text, str) or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError("HyperFrames transcript의 text, start, end 형식이 올바르지 않습니다.")
        if start < 0 or end <= start:
            raise ValueError("HyperFrames transcript의 시간 범위가 올바르지 않습니다.")
        duration = max(duration, float(end))
        cues.append(
            f'''      <div id="caption-{index}" class="caption clip" data-start="{float(start)}" data-duration="{float(end - start)}" data-track-index="5">{escape(text)}</div>'''
        )
        animations.append(
            f'''      window.__timelines["main"].fromTo("#caption-{index}", {{ opacity: 0, y: 28, scale: 0.97 }}, {{ opacity: 1, y: 0, scale: 1, duration: 0.18, ease: "power2.out" }}, {float(start)});'''
        )

    if duration <= 0:
        raise ValueError("자막이 없는 HyperFrames composition에는 영상 길이가 필요합니다.")

    escaped_video_filename = escape(video_filename, quote=True)
    return f'''<!doctype html>
<html lang="ko" data-resolution="portrait">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={width}, height={height}" />
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      * {{ box-sizing: border-box; }}
      html, body {{ margin: 0; width: {width}px; height: {height}px; overflow: hidden; background: #000; }}
      .caption {{
        position: absolute;
        left: 80px;
        right: 80px;
        bottom: 260px;
        padding: 24px 32px;
        color: #fff;
        background: rgba(0, 0, 0, 0.62);
        border-radius: 18px;
        font: 700 56px/1.2 Inter, sans-serif;
        text-align: center;
      }}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{duration}" data-width="{width}" data-height="{height}" data-fps="{fps}">
      <video id="a-roll" class="clip" src="{escaped_video_filename}" muted playsinline data-start="0" data-duration="{duration}" data-track-index="0" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover"></video>
      <audio id="a-roll-audio" src="{escaped_video_filename}" data-start="0" data-duration="{duration}" data-track-index="2" data-volume="1"></audio>
{chr(10).join(cues)}
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      window.__timelines["main"] = gsap.timeline({{ paused: true }});
{chr(10).join(animations)}
      // Keep the registered timeline seekable across the entire composition.
      window.__timelines["main"].fromTo(
        "#root",
        {{ scale: 1 }},
        {{ scale: 1.01, duration: {duration}, ease: "none" }},
        0
      );
    </script>
  </body>
</html>
'''
