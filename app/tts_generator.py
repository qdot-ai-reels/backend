"""Generate one narration audio file from a complete reels script."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


class TTSGenerationError(RuntimeError):
    """Raised when Google TTS cannot generate the narration."""


class TTSConfigurationError(TTSGenerationError):
    """Raised when the local Google TTS configuration is incomplete."""


class NarrationValidationError(TTSGenerationError):
    """Raised when a script does not contain usable narration text."""


class SceneAudioDurationError(TTSGenerationError):
    """Raised when generated speech does not fit its scene time range."""

    def __init__(
        self,
        scene_number: int,
        expected_seconds: float,
        actual_seconds: float,
    ) -> None:
        self.scene_number = scene_number
        self.expected_seconds = expected_seconds
        self.actual_seconds = actual_seconds
        self.retryable = True
        self.next_step = "regenerate_script"
        super().__init__(
            f"{scene_number}번째 장면 음성이 너무 깁니다. "
            f"허용 시간: {expected_seconds:.2f}초, "
            f"실제 음성: {actual_seconds:.2f}초"
        )


@dataclass(frozen=True)
class SceneNarration:
    scene_number: int
    start_seconds: float
    end_seconds: float
    text: str
    audio_content: bytes | None = None


def build_scene_narrations(script: Mapping[str, Any]) -> list[SceneNarration]:
    """Extract each scene's voiceover and its requested timeline."""
    scenes = script.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise NarrationValidationError("스크립트에는 하나 이상의 scenes가 필요합니다.")

    narrations = []
    previous_end = 0.0
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, Mapping):
            raise NarrationValidationError(f"{index}번째 scene이 JSON 객체가 아닙니다.")
        time_range = scene.get("time_range_sec")
        if isinstance(time_range, Mapping):
            start = time_range.get("start")
            end = time_range.get("end")
        else:
            start = end = None
        if (
            not isinstance(time_range, Mapping)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or start < previous_end
            or end <= start
        ):
            raise NarrationValidationError(
                f"{index}번째 scene의 time_range_sec가 올바르지 않습니다."
            )
        auditory = scene.get("auditory")
        if not isinstance(auditory, Mapping):
            raise NarrationValidationError(f"{index}번째 scene의 auditory가 필요합니다.")
        voiceover = auditory.get("voiceover")
        if voiceover is None:
            voiceover = ""
        elif not isinstance(voiceover, str):
            raise NarrationValidationError(
                f"{index}번째 scene의 voiceover는 문자열 또는 null이어야 합니다."
            )
        narrations.append(
            SceneNarration(
                scene_number=index,
                start_seconds=float(start),
                end_seconds=float(end),
                text=voiceover.strip(),
            )
        )
        previous_end = float(end)

    return narrations


@dataclass(frozen=True)
class GoogleTTSSettings:
    language_code: str = "ko-KR"
    voice_name: str = "ko-KR-Standard-A"
    syllables_per_second: float = 4.5

    @classmethod
    def from_env(cls) -> "GoogleTTSSettings":
        return cls(
            language_code=os.getenv("GOOGLE_TTS_LANGUAGE_CODE", "ko-KR"),
            voice_name=os.getenv("GOOGLE_TTS_VOICE_NAME", "ko-KR-Standard-A"),
            syllables_per_second=float(
                os.getenv("GOOGLE_TTS_SYLLABLES_PER_SECOND", "4.5")
            ),
        )


class GoogleTTSClient:
    """Create scene audio tracks and combine them into one MP3 narration."""

    def __init__(
        self,
        synthesizer: Callable[[str], bytes] | None = None,
        combiner: Callable[[Sequence[SceneNarration]], bytes] | None = None,
        duration_reader: Callable[[bytes], float] | None = None,
        duration_tolerance_seconds: float = 0.1,
        settings: GoogleTTSSettings | None = None,
    ) -> None:
        if duration_tolerance_seconds < 0:
            raise ValueError("duration_tolerance_seconds는 0 이상이어야 합니다.")
        self.settings = settings or GoogleTTSSettings.from_env()
        self.synthesizer = synthesizer or self._create_google_synthesizer()
        self.combiner = combiner or combine_scene_audio
        self.duration_reader = duration_reader or read_audio_duration
        self.duration_tolerance_seconds = duration_tolerance_seconds

    def generate_narration(self, script: Mapping[str, Any]) -> bytes:
        scene_narrations = build_scene_narrations(script)
        generated = []
        for narration in scene_narrations:
            if not narration.text:
                generated.append(narration)
                continue
            try:
                audio_content = self.synthesizer(narration.text)
            except TTSGenerationError:
                raise
            except Exception as error:
                raise TTSGenerationError(
                    f"{narration.scene_number}번째 장면의 Google TTS 생성에 실패했습니다."
                ) from error
            expected_seconds = narration.end_seconds - narration.start_seconds
            actual_seconds = self.duration_reader(audio_content)
            if actual_seconds > expected_seconds + self.duration_tolerance_seconds:
                raise SceneAudioDurationError(
                    scene_number=narration.scene_number,
                    expected_seconds=expected_seconds,
                    actual_seconds=actual_seconds,
                )

            generated.append(
                SceneNarration(
                    scene_number=narration.scene_number,
                    start_seconds=narration.start_seconds,
                    end_seconds=narration.end_seconds,
                    text=narration.text,
                    audio_content=audio_content,
                )
            )

        try:
            combined_audio = self.combiner(generated)
        except TTSGenerationError:
            raise
        except Exception as error:
            raise TTSGenerationError("장면별 음성 결합에 실패했습니다.") from error

        if not combined_audio:
            raise TTSGenerationError("음성 결합 결과가 비어 있습니다.")
        return combined_audio

    def _create_google_synthesizer(self) -> Callable[[str], bytes]:
        try:
            from google.cloud import texttospeech
        except ImportError as error:
            raise TTSConfigurationError(
                "google-cloud-texttospeech 패키지가 설치되지 않았습니다."
            ) from error

        try:
            client = texttospeech.TextToSpeechClient()
        except Exception as error:
            raise TTSConfigurationError(
                "Google TTS 인증 설정을 확인할 수 없습니다."
            ) from error

        def synthesize(text: str) -> bytes:
            response = client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=text),
                voice=texttospeech.VoiceSelectionParams(
                    language_code=self.settings.language_code,
                    name=self.settings.voice_name,
                ),
                audio_config=texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.MP3,
                ),
            )
            return response.audio_content

        return synthesize


def read_audio_duration(audio_content: bytes) -> float:
    """Read generated MP3 duration with FFprobe."""
    with tempfile.NamedTemporaryFile(suffix=".mp3") as audio_file:
        audio_file.write(audio_content)
        audio_file.flush()
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    audio_file.name,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as error:
            raise TTSGenerationError("FFprobe가 설치되지 않았습니다.") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr[-500:]
            raise TTSGenerationError(f"음성 길이를 읽지 못했습니다: {detail}") from error

    try:
        return float(result.stdout.strip())
    except ValueError as error:
        raise TTSGenerationError("FFprobe가 올바른 음성 길이를 반환하지 않았습니다.") from error


def combine_scene_audio(scene_narrations: Sequence[SceneNarration]) -> bytes:
    """Overlay scene MP3 files at their start times and preserve silence."""
    if not scene_narrations:
        raise TTSGenerationError("결합할 장면별 음성 데이터가 없습니다.")

    total_duration = max(narration.end_seconds for narration in scene_narrations)
    if total_duration <= 0:
        raise TTSGenerationError("결합할 음성의 전체 시간이 올바르지 않습니다.")

    with tempfile.TemporaryDirectory(prefix="quedot-tts-") as directory:
        directory_path = Path(directory)
        input_paths = []
        for index, narration in enumerate(scene_narrations):
            if narration.audio_content is None:
                continue
            input_path = directory_path / f"scene_{index:03d}.mp3"
            input_path.write_bytes(narration.audio_content)
            input_paths.append(input_path)

        filter_parts = [
            f"[0:a]atrim=duration={total_duration:.3f}[silence]"
        ]
        audio_labels = []
        audio_labels.append("[silence]")
        audio_index = 1
        for narration in scene_narrations:
            if narration.audio_content is None:
                continue
            delay_ms = round(narration.start_seconds * 1000)
            label = f"a{audio_index}"
            filter_parts.append(f"[{audio_index}:a]adelay={delay_ms}:all=1[{label}]")
            audio_labels.append(f"[{label}]")
            audio_index += 1
        filter_parts.append(
            "".join(audio_labels)
            + f"amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0,"
            f"atrim=duration={total_duration:.3f}[out]"
        )

        output_path = directory_path / "narration.mp3"
        command = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-t",
            f"{total_duration:.3f}",
            "-i",
            "anullsrc=r=24000:cl=mono",
        ]
        for input_path in input_paths:
            command.extend(["-i", str(input_path)])
        command.extend([
            "-filter_complex", ";".join(filter_parts),
            "-map", "[out]",
            "-c:a", "libmp3lame",
            "-b:a", "128k",
            "-t", f"{total_duration:.3f}",
            str(output_path),
        ])

        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise TTSGenerationError("FFmpeg가 설치되지 않았습니다.") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.decode("utf-8", errors="replace")[-500:]
            raise TTSGenerationError(f"FFmpeg 음성 결합에 실패했습니다: {detail}") from error

        return output_path.read_bytes()
