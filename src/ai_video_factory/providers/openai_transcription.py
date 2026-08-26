from typing import TYPE_CHECKING, Any

from ai_video_factory.providers.transcription import TranscribedWord

if TYPE_CHECKING:
    from openai import AsyncOpenAI


class OpenAITranscriptionProvider:
    """OpenAI implementation for extracting word-level narration timestamps."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: "AsyncOpenAI | Any | None" = None,
    ) -> None:
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key)

        self._client = client

    async def transcribe_words(
        self,
        audio: bytes,
        *,
        filename: str,
        model: str,
        prompt: str,
        language: str | None,
    ) -> list[TranscribedWord]:
        kwargs: dict[str, Any] = {
            "file": (filename, audio, "audio/wav"),
            "model": model,
            "prompt": prompt,
            "response_format": "verbose_json",
            "timestamp_granularities": ["word"],
            "temperature": 0,
        }
        if language is not None:
            kwargs["language"] = language

        response = await self._client.audio.transcriptions.create(**kwargs)
        raw_words = getattr(response, "words", None)
        if not raw_words:
            raise RuntimeError("OpenAI transcription returned no word timestamps")

        words: list[TranscribedWord] = []
        for raw_word in raw_words:
            text = getattr(raw_word, "word", None)
            start = getattr(raw_word, "start", None)
            end = getattr(raw_word, "end", None)
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("OpenAI transcription returned an invalid word")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                raise RuntimeError("OpenAI transcription returned invalid word timestamps")

            words.append(
                TranscribedWord(
                    text=text,
                    start_seconds=float(start),
                    end_seconds=float(end),
                )
            )

        return words
