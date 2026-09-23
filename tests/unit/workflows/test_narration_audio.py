import asyncio
import io
import wave
from pathlib import Path
from typing import Any

import pytest

from ai_video_factory.domain import SourceScript
from ai_video_factory.providers.speech import GeneratedSpeech
from ai_video_factory.workflows.narration_audio import generate_narration_audio


def _make_wav_bytes(*, duration_seconds: float = 0.25, sample_rate: int = 16_000) -> bytes:
    frame_count = int(duration_seconds * sample_rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * frame_count)
    return buffer.getvalue()


def _make_streaming_wav_bytes(
    *,
    duration_seconds: float = 0.25,
    sample_rate: int = 24_000,
) -> bytes:
    content = bytearray(
        _make_wav_bytes(
            duration_seconds=duration_seconds,
            sample_rate=sample_rate,
        )
    )
    data_chunk_index = content.find(b"data")
    if data_chunk_index < 0:
        raise AssertionError("Test WAV contains no data chunk")

    content[data_chunk_index + 4 : data_chunk_index + 8] = (0xFFFFFFFF).to_bytes(4, "little")
    return bytes(content)


class RecordingSpeechProvider:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.last_call: dict[str, Any] | None = None

    async def generate_speech(self, **kwargs: Any) -> GeneratedSpeech:
        self.last_call = kwargs
        return GeneratedSpeech(
            content=self.payload,
            media_type="audio/wav",
            extension="wav",
        )


def test_narration_workflow_preserves_source_text_and_measures_real_duration(
    tmp_path: Path,
) -> None:
    source_text = "Japón es gobernado durante siglos por guerreros.\nAparecen los samuráis."
    provider = RecordingSpeechProvider(_make_wav_bytes(duration_seconds=0.4))
    output_dir = tmp_path / "phase5"

    narration = asyncio.run(
        generate_narration_audio(
            SourceScript(text=source_text),
            speech_provider=provider,  # type: ignore[arg-type]
            output_dir=output_dir,
            model="test-tts",
            voice="test-voice",
            instructions="Natural documentary narration.",
            speed=1.0,
        )
    )

    assert provider.last_call is not None
    assert provider.last_call["text"] == source_text
    assert provider.last_call["output_format"] == "wav"
    assert narration.uri == "narration.wav"
    assert narration.duration_seconds == pytest.approx(0.4)
    assert (output_dir / "narration.wav").read_bytes() == provider.payload


def test_narration_workflow_measures_actual_frames_for_streaming_wav_header(
    tmp_path: Path,
) -> None:
    provider = RecordingSpeechProvider(
        _make_streaming_wav_bytes(duration_seconds=0.4, sample_rate=24_000)
    )
    output_dir = tmp_path / "phase5"

    narration = asyncio.run(
        generate_narration_audio(
            SourceScript(text="Narración de prueba."),
            speech_provider=provider,  # type: ignore[arg-type]
            output_dir=output_dir,
            model="test-tts",
            voice="test-voice",
            instructions="Natural narration.",
            speed=1.0,
        )
    )

    assert narration.duration_seconds == pytest.approx(0.4)
    assert (output_dir / "narration.wav").read_bytes() == provider.payload


def test_narration_workflow_writes_nothing_for_invalid_wav(tmp_path: Path) -> None:
    provider = RecordingSpeechProvider(b"not-a-wav")
    output_dir = tmp_path / "phase5"

    with pytest.raises(ValueError, match="invalid WAV"):
        asyncio.run(
            generate_narration_audio(
                SourceScript(text="Narración válida."),
                speech_provider=provider,  # type: ignore[arg-type]
                output_dir=output_dir,
                model="test-tts",
                voice="test-voice",
                instructions="Natural narration.",
                speed=1.0,
            )
        )

    assert not output_dir.exists()
