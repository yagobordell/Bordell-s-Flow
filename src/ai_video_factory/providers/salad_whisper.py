from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.workers.whisper import (
    WHISPER_GENERATION_PROFILE,
    WHISPER_MODEL_ID,
    WHISPER_TRANSCRIPTION_TASK,
    WhisperTranscript,
    whisper_application_job_id,
)

from .inference_jobs import InferenceJobExecutor, cached_inference_response
from .transcription import TranscribedWord


def build_whisper_job_request(
    *,
    audio_sha256: str,
    filename: str,
    model: str,
    language: str | None,
) -> InferenceJobRequest:
    """Build the canonical deterministic Whisper request for audit and execution."""

    if model != WHISPER_MODEL_ID:
        raise ValueError(
            f"Dedicated Whisper worker only supports model {WHISPER_MODEL_ID!r}, got {model!r}"
        )
    suffix = Path(filename).suffix or ".wav"
    job_id = whisper_application_job_id(
        audio_sha256=audio_sha256,
        language=language,
        model_id=model,
    )
    input_key = f"phase5/whisper/inputs/{audio_sha256}{suffix.lower()}"
    return InferenceJobRequest(
        job_id=job_id,
        task=WHISPER_TRANSCRIPTION_TASK,
        inputs=[
            ObjectInput(
                name="audio",
                key=input_key,
                sha256=audio_sha256,
                content_type="audio/wav",
            )
        ],
        output=ObjectOutput(
            key=f"jobs/{job_id}/words.json",
            content_type="application/json",
        ),
        parameters={
            "generation_profile": WHISPER_GENERATION_PROFILE,
            "model_id": model,
            "language": language,
        },
    )


class SaladWhisperTranscriptionProvider:
    """Transcription provider backed by the dedicated Salad Whisper worker."""

    def __init__(
        self,
        *,
        executor: InferenceJobExecutor,
        temp_dir: Path,
    ) -> None:
        self._executor = executor
        self._temp_dir = temp_dir

    async def transcribe_words(
        self,
        audio: bytes,
        *,
        filename: str,
        model: str,
        prompt: str,
        language: str | None,
    ) -> list[TranscribedWord]:
        return await asyncio.to_thread(
            self._transcribe_words_sync,
            audio,
            filename=filename,
            model=model,
            prompt=prompt,
            language=language,
        )

    def _transcribe_words_sync(
        self,
        audio: bytes,
        *,
        filename: str,
        model: str,
        prompt: str,
        language: str | None,
    ) -> list[TranscribedWord]:
        if model != WHISPER_MODEL_ID:
            raise ValueError(
                f"Dedicated Whisper worker only supports model {WHISPER_MODEL_ID!r}, got {model!r}"
            )
        if not audio:
            raise ValueError("Whisper transcription audio must be non-empty")

        # The known narration text remains source truth for downstream validation.
        # Do not feed it into Whisper decoder prompt_ids: this long-form ASR path
        # intentionally transcribes only from audio + explicit language.
        del prompt

        self._temp_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix or ".wav"
        with tempfile.TemporaryDirectory(prefix="whisper-client-", dir=self._temp_dir) as tmp:
            work_dir = Path(tmp)
            audio_path = work_dir / f"input{suffix}"
            audio_path.write_bytes(audio)
            audio_sha256 = sha256_file(audio_path)
            request = build_whisper_job_request(
                audio_sha256=audio_sha256,
                filename=filename,
                model=model,
                language=language,
            )
            response = cached_inference_response(self._executor.storage, request)
            if response is None:
                audio_input = request.inputs[0]
                self._executor.ensure_input(
                    audio_path,
                    key=audio_input.key,
                    sha256=audio_sha256,
                    content_type="audio/wav",
                    metadata={"stage": "phase5-alignment"},
                )
                response = self._executor.execute(
                    request,
                    metadata={
                        "application_job_id": request.job_id,
                        "stage": "phase5-alignment",
                        "model": model,
                    },
                )
            output_path = work_dir / "words.json"
            self._executor.download_output(response, output_path)
            transcript = WhisperTranscript.model_validate_json(
                output_path.read_text(encoding="utf-8")
            )

        if transcript.model_id != model:
            raise RuntimeError("Whisper transcript model_id does not match the requested model")
        return [
            TranscribedWord(
                text=word.text,
                start_seconds=word.start_seconds,
                end_seconds=word.end_seconds,
            )
            for word in transcript.words
        ]
