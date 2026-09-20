import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ai_video_factory.domain import NarrationAudio, SourceScript
from ai_video_factory.providers.openai_transcription import OpenAITranscriptionProvider
from ai_video_factory.providers.transcription import TranscribedWord
from ai_video_factory.workflows.narration_alignment import align_narration_words


class FakeTranscriptionsResource:
    def __init__(self) -> None:
        self.last_call: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.last_call = kwargs
        return SimpleNamespace(
            words=[
                SimpleNamespace(word="Japón", start=0.2, end=0.7),
                SimpleNamespace(word="feudal", start=0.8, end=1.4),
            ]
        )


class FakeAudioResource:
    def __init__(self) -> None:
        self.transcriptions = FakeTranscriptionsResource()


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.audio = FakeAudioResource()


def test_openai_transcription_provider_requests_word_timestamps() -> None:
    client = FakeOpenAIClient()
    provider = OpenAITranscriptionProvider(client=client)  # type: ignore[arg-type]

    words = asyncio.run(
        provider.transcribe_words(
            b"wav-bytes",
            filename="narration.wav",
            model="whisper-1",
            prompt="Japón feudal",
            language="es",
        )
    )

    assert words == [
        TranscribedWord(text="Japón", start_seconds=0.2, end_seconds=0.7),
        TranscribedWord(text="feudal", start_seconds=0.8, end_seconds=1.4),
    ]
    assert client.audio.transcriptions.last_call == {
        "file": ("narration.wav", b"wav-bytes", "audio/wav"),
        "model": "whisper-1",
        "prompt": "Japón feudal",
        "response_format": "verbose_json",
        "timestamp_granularities": ["word"],
        "temperature": 0,
        "language": "es",
    }


class RecordingTranscriptionProvider:
    def __init__(self, words: list[TranscribedWord]) -> None:
        self.words = words
        self.last_call: dict[str, Any] | None = None

    async def transcribe_words(self, audio: bytes, **kwargs: Any) -> list[TranscribedWord]:
        self.last_call = {"audio": audio, **kwargs}
        return self.words


def test_alignment_preserves_source_as_prompt_and_assigns_word_ids() -> None:
    source_text = "Japón fue gobernado por guerreros.\nAparecen los samuráis."
    provider = RecordingTranscriptionProvider(
        [
            TranscribedWord(text=" Japón ", start_seconds=0.2, end_seconds=0.7),
            TranscribedWord(text="fue", start_seconds=0.8, end_seconds=1.0),
            TranscribedWord(text="gobernado", start_seconds=1.1, end_seconds=1.6),
        ]
    )

    words = asyncio.run(
        align_narration_words(
            SourceScript(text=source_text),
            NarrationAudio(uri="narration.wav", duration_seconds=2.0),
            b"audio",
            transcription_provider=provider,  # type: ignore[arg-type]
            model="whisper-1",
            language="es",
        )
    )

    assert provider.last_call is not None
    assert provider.last_call["prompt"] == source_text
    assert provider.last_call["filename"] == "narration.wav"
    assert [word.model_dump() for word in words] == [
        {"id": 1, "text": "Japón", "start_seconds": 0.2, "end_seconds": 0.7},
        {"id": 2, "text": "fue", "start_seconds": 0.8, "end_seconds": 1.0},
        {"id": 3, "text": "gobernado", "start_seconds": 1.1, "end_seconds": 1.6},
    ]


def test_alignment_accepts_zero_duration_word_timestamp() -> None:
    provider = RecordingTranscriptionProvider(
        [
            TranscribedWord(text="Japón", start_seconds=0.0, end_seconds=0.0),
            TranscribedWord(text="feudal", start_seconds=0.2, end_seconds=0.8),
        ]
    )

    words = asyncio.run(
        align_narration_words(
            SourceScript(text="Japón feudal"),
            NarrationAudio(uri="narration.wav", duration_seconds=1.0),
            b"audio",
            transcription_provider=provider,  # type: ignore[arg-type]
            model="whisper-1",
            language="es",
        )
    )

    assert [word.model_dump() for word in words] == [
        {"id": 1, "text": "Japón", "start_seconds": 0.0, "end_seconds": 0.0},
        {"id": 2, "text": "feudal", "start_seconds": 0.2, "end_seconds": 0.8},
    ]


def test_alignment_rejects_end_before_start_with_diagnostics() -> None:
    provider = RecordingTranscriptionProvider(
        [TranscribedWord(text="error", start_seconds=1.0, end_seconds=0.9)]
    )

    with pytest.raises(ValueError, match=r"word_id=1.*text='error'.*start=1.0.*end=0.9"):
        asyncio.run(
            align_narration_words(
                SourceScript(text="error"),
                NarrationAudio(uri="narration.wav", duration_seconds=2.0),
                b"audio",
                transcription_provider=provider,  # type: ignore[arg-type]
                model="whisper-1",
                language=None,
            )
        )


def test_alignment_rejects_words_beyond_measured_narration_duration() -> None:
    provider = RecordingTranscriptionProvider(
        [TranscribedWord(text="fin", start_seconds=1.8, end_seconds=2.7)]
    )

    with pytest.raises(ValueError, match="exceed measured narration duration"):
        asyncio.run(
            align_narration_words(
                SourceScript(text="fin"),
                NarrationAudio(uri="narration.wav", duration_seconds=2.0),
                b"audio",
                transcription_provider=provider,  # type: ignore[arg-type]
                model="whisper-1",
                language=None,
            )
        )


def test_alignment_rejects_unordered_word_timestamps() -> None:
    provider = RecordingTranscriptionProvider(
        [
            TranscribedWord(text="uno", start_seconds=1.0, end_seconds=1.4),
            TranscribedWord(text="dos", start_seconds=0.9, end_seconds=1.5),
        ]
    )

    with pytest.raises(ValueError, match="must be ordered"):
        asyncio.run(
            align_narration_words(
                SourceScript(text="uno dos"),
                NarrationAudio(uri="narration.wav", duration_seconds=2.0),
                b"audio",
                transcription_provider=provider,  # type: ignore[arg-type]
                model="whisper-1",
                language=None,
            )
        )


def test_alignment_rejects_implausible_word_count_inflation() -> None:
    source_text = "uno dos tres cuatro cinco seis siete ocho nueve diez"
    provider = RecordingTranscriptionProvider(
        [
            TranscribedWord(
                text=f"palabra{index}",
                start_seconds=index * 0.05,
                end_seconds=index * 0.05 + 0.04,
            )
            for index in range(20)
        ]
    )

    with pytest.raises(
        ValueError,
        match=r"word count is implausible.*source_words=10.*aligned_words=20",
    ):
        asyncio.run(
            align_narration_words(
                SourceScript(text=source_text),
                NarrationAudio(uri="narration.wav", duration_seconds=2.0),
                b"audio",
                transcription_provider=provider,  # type: ignore[arg-type]
                model="whisper-1",
                language="es",
            )
        )


def test_alignment_rejects_long_trailing_zero_duration_run() -> None:
    provider = RecordingTranscriptionProvider(
        [
            TranscribedWord(text="uno", start_seconds=0.0, end_seconds=0.2),
            TranscribedWord(text="dos", start_seconds=0.2, end_seconds=0.4),
            TranscribedWord(text="tres", start_seconds=0.4, end_seconds=0.4),
            TranscribedWord(text="cuatro", start_seconds=0.4, end_seconds=0.4),
            TranscribedWord(text="cinco", start_seconds=0.4, end_seconds=0.4),
            TranscribedWord(text="seis", start_seconds=0.4, end_seconds=0.4),
            TranscribedWord(text="siete", start_seconds=0.4, end_seconds=0.4),
        ]
    )

    with pytest.raises(
        ValueError,
        match=r"trailing zero-duration word run.*count=5",
    ):
        asyncio.run(
            align_narration_words(
                SourceScript(text="uno dos tres cuatro cinco seis siete"),
                NarrationAudio(uri="narration.wav", duration_seconds=1.0),
                b"audio",
                transcription_provider=provider,  # type: ignore[arg-type]
                model="whisper-1",
                language="es",
            )
        )
