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
      #root {{ position: relative; width: {width}px; height: {height}px; overflow: hidden; }}
      .caption {{
        position: absolute;
        left: 50%;
        width: min(880px, calc(100% - 144px));
        bottom: 300px;
        transform: translateX(-50%);
        padding: 18px 30px 20px;
        color: #fff;
        background: rgba(8, 8, 10, 0.48);
        border: 1px solid rgba(255, 255, 255, 0.18);
        border-radius: 22px;
        box-shadow: 0 12px 36px rgba(0, 0, 0, 0.28);
        backdrop-filter: blur(8px);
        font: 800 58px/1.18 Pretendard, "Apple SD Gothic Neo", sans-serif;
        letter-spacing: -1.4px;
        text-align: center;
        text-wrap: balance;
        white-space: pre-line;
        max-height: 178px;
        overflow: hidden;
        text-shadow: 0 2px 7px rgba(0, 0, 0, 0.72);
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
      // Keep the registered timeline seekable across the entire composition,
      // including videos whose captions finish animating near the first frame.
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
