"""OpenAI word-timestamp transcription for independently generated Fish tracks."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

_MAX_AUDIO_BYTES = 25 * 1024 * 1024


class OpenAISttError(RuntimeError):
    """Audio cannot be transcribed with verifiable word timestamps."""


class OpenAISttClient:
    """Synchronous client invoked in a thread so TTS blocks stay concurrent."""

    def __init__(
        self, api_key: str | None = None, *, client: Any | None = None,
        model: str = "whisper-1",
    ) -> None:
        if model != "whisper-1":
            raise ValueError("Only whisper-1 currently provides the required word timestamps")
        if client is None:
            if not api_key:
                raise OpenAISttError("OPENAI_API_KEY is required for OpenAI STT")
            from openai import OpenAI

            # Avoid SDK automatic retries after an ambiguous, billable POST.
            client = OpenAI(api_key=api_key, max_retries=0)
        self.client = client
        self.model = model

    def transcribe(self, audio: Path) -> dict[str, Any]:
        if audio.is_symlink() or not audio.is_file():
            raise OpenAISttError(f"Missing or linked source audio: {audio}")
        if not 0 < audio.stat().st_size <= _MAX_AUDIO_BYTES:
            raise OpenAISttError("OpenAI STT audio must be nonempty and at most 25 MiB")
        with audio.open("rb") as stream:
            response = self.client.audio.transcriptions.create(
                file=stream,
                model=self.model,
                response_format="verbose_json",
                timestamp_granularities=["word"],
            )
        raw = response if isinstance(response, dict) else response.model_dump(mode="json")
        words = raw.get("words")
        if not isinstance(words, list) or not words:
            raise OpenAISttError("OpenAI STT returned no word timestamps")
        normalized: list[dict[str, Any]] = []
        previous_start = 0.0
        for index, entry in enumerate(words):
            if not isinstance(entry, dict):
                raise OpenAISttError("Malformed OpenAI STT word")
            word, start, end = entry.get("word"), entry.get("start"), entry.get("end")
            if (
                not isinstance(word, str) or not word.strip()
                or isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end < start or (index and start < previous_start)
            ):
                raise OpenAISttError(f"Invalid OpenAI STT word timing at index {index}")
            normalized.append({"word": word, "start": float(start), "end": float(end)})
            previous_start = float(start)
        return {
            "model": self.model,
            "text": raw.get("text"),
            "language": raw.get("language"),
            "words": normalized,
        }
