"""OpenAI word-timestamp transcription for independently generated Fish tracks."""

from __future__ import annotations

import json
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

    def _raw_transcript(self, audio: Path) -> dict[str, Any]:
        if audio.is_symlink() or not audio.is_file():
            raise OpenAISttError(f"Missing or linked source audio: {audio}")
        if not 0 < audio.stat().st_size <= _MAX_AUDIO_BYTES:
            raise OpenAISttError("OpenAI STT audio must be nonempty and at most 25 MiB")
        with audio.open("rb") as stream:
            response = self.client.audio.transcriptions.create(
                file=stream, model=self.model,
                response_format="verbose_json",
                timestamp_granularities=["word"],
            )
        return response if isinstance(response, dict) else response.model_dump(mode="json")

    def transcribe(self, audio: Path) -> dict[str, Any]:
        """Regular STT; persistent workflows use the durable-response variant."""
        return self._normalize_raw(self._raw_transcript(audio))

    def transcribe_durable(
        self, audio: Path, *, response_path: Path, source_audio_sha256: str,
    ) -> dict[str, Any]:
        """Persist the paid response before parsing so validation is replayable."""
        temporary = response_path.with_name(response_path.name + ".tmp")
        if any(path.is_symlink() for path in (
            response_path.parent, response_path, temporary,
        )):
            raise OpenAISttError("Refusing linked OpenAI STT response cache")
        if response_path.is_file():
            saved = json.loads(response_path.read_text(encoding="utf-8"))
            if (
                not isinstance(saved, dict)
                or saved.get("schema_version") != "openai-stt-raw-v1"
                or saved.get("source_audio_sha256") != source_audio_sha256
                or saved.get("model") != self.model
                or not isinstance(saved.get("response"), dict)
            ):
                raise OpenAISttError("Saved raw STT response differs from source or model")
            return self._normalize_raw(saved["response"])
        if response_path.exists() or temporary.exists():
            raise OpenAISttError("Unexpected raw STT cache path or interrupted write")
        raw = self._raw_transcript(audio)
        response_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            temporary.write_text(
                json.dumps({
                    "schema_version": "openai-stt-raw-v1",
                    "source_audio_sha256": source_audio_sha256,
                    "model": self.model,
                    "response": raw,
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temporary.replace(response_path)
        finally:
            temporary.unlink(missing_ok=True)
        return self._normalize_raw(raw)

    def _normalize_raw(self, raw: dict[str, Any]) -> dict[str, Any]:
        words = raw.get("words")
        if not isinstance(words, list) or not words:
            raise OpenAISttError("OpenAI STT returned no word timestamps")
        # Whisper occasionally omits or corrupts ONE word timestamp in an
        # otherwise valid transcript. Recover only bounded gaps using adjacent
        # timestamps, keeping every spoken token in its original order.
        def timestamp(value: Any) -> float | None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            number = float(value)
            return number if math.isfinite(number) and number >= 0 else None

        parsed: list[dict[str, Any]] = []
        skipped_nonlexical: list[int] = []
        for index, entry in enumerate(words):
            if not isinstance(entry, dict):
                raise OpenAISttError(f"Malformed OpenAI STT word at index {index}")
            word = entry.get("word")
            if not isinstance(word, str):
                raise OpenAISttError(
                    f"Invalid OpenAI STT word at index {index}: "
                    f"non-text word ({type(word).__name__})"
                )
            # Only nonspoken pseudo-words are skipped; all lexical content
            # still enters strict downstream B2/STT alignment.
            if not any(character.isalnum() for character in word):
                skipped_nonlexical.append(index)
                continue
            parsed.append({
                "source_index": index,
                "word": word,
                "start": timestamp(entry.get("start")),
                "end": timestamp(entry.get("end")),
            })
        if not parsed:
            raise OpenAISttError("OpenAI STT returned no spoken word timestamps")

        normalized: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        previous_start = 0.0
        # Never infer a word's position across a long pause or an unanchored
        # region: such a transcript needs another real STT response.
        max_gap = 2.0
        for position, entry in enumerate(parsed):
            index = entry["source_index"]
            start, end = entry["start"], entry["end"]
            recovered_fields: list[str] = []
            if start is None:
                lower = normalized[-1]["end"] if normalized else 0.0
                upper = end
                if upper is None:
                    upper = next(
                        (later["start"] for later in parsed[position + 1:]
                         if later["start"] is not None),
                        None,
                    )
                if (
                    upper is None or upper < lower
                    or upper - lower > max_gap
                ):
                    raise OpenAISttError(
                        f"Unrecoverable OpenAI STT start at index {index}: "
                        "no bounded neighboring timestamps"
                    )
                start = lower
                recovered_fields.append("start")
            safe_start = max(start, previous_start)
            if end is None:
                next_start = next(
                    (later["start"] for later in parsed[position + 1:]
                     if later["start"] is not None),
                    None,
                )
                if (
                    next_start is not None and next_start >= safe_start
                    and next_start - safe_start <= max_gap
                ):
                    end = next_start
                elif entry["start"] is not None:
                    # Zero-length words are already supported by the timeline;
                    # an unknown end must never shift a later known start.
                    end = safe_start
                else:
                    raise OpenAISttError(
                        f"Unrecoverable OpenAI STT end at index {index}: "
                        "no bounded neighboring timestamps"
                    )
                recovered_fields.append("end")
            safe_end = max(end, safe_start)
            normalized.append({
                "word": entry["word"], "start": safe_start, "end": safe_end,
            })
            if recovered_fields:
                repairs.append({"index": index, "fields": recovered_fields})
            previous_start = safe_start
        return {
            "model": self.model,
            "text": raw.get("text"),
            "language": raw.get("language"),
            "words": normalized,
            **({"timing_repairs": repairs} if repairs else {}),
            **({"skipped_nonlexical_indices": skipped_nonlexical}
               if skipped_nonlexical else {}),
        }
