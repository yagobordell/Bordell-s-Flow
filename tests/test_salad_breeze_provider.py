import asyncio
import hashlib
import io
import wave
from pathlib import Path

import pytest

from ai_video_factory.inference.contracts import InferenceJobResponse, OutputArtifact
from ai_video_factory.providers.inference_jobs import InferenceTransportFailedError
from ai_video_factory.providers.job_queue import QueueJobStatus
from ai_video_factory.providers.salad_breeze import (
    BreezeFallbackEligibleError,
    SaladBreezeSpeechProvider,
)
from ai_video_factory.workers.breeze_tts2 import BREEZE_TTS2_MODEL_ID, BREEZE_TTS2_TASK


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24_000)
        wav_file.writeframes(b"\x00\x00" * 2400)
    return buffer.getvalue()


class FakeExecutor:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.request = None
        self.metadata = None

    def execute(self, request, *, metadata):
        self.request = request
        self.metadata = metadata
        return InferenceJobResponse(
            job_id=request.job_id,
            request_sha256=request.fingerprint(),
            output=OutputArtifact(
                key=request.output.key,
                content_type="audio/wav",
                size_bytes=len(self.payload),
                sha256=hashlib.sha256(self.payload).hexdigest(),
            ),
            attempt_count=1,
        )

    def download_output(self, response, destination: Path) -> None:
        destination.write_bytes(self.payload)


def test_salad_breeze_provider_builds_prompt_only_inference_job(tmp_path: Path) -> None:
    payload = _wav_bytes()
    executor = FakeExecutor(payload)
    provider = SaladBreezeSpeechProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        cfg_scale=4.0,
        seed=42,
    )

    speech = asyncio.run(
        provider.generate_speech(
            text="A canonical English narration.",
            model=BREEZE_TTS2_MODEL_ID,
            voice="A warm documentary narrator.",
            instructions="Measured, restrained delivery.",
            speed=1.0,
            output_format="wav",
        )
    )

    assert speech.content == payload
    assert speech.media_type == "audio/wav"
    assert speech.extension == "wav"
    assert executor.request is not None
    assert executor.request.task == BREEZE_TTS2_TASK
    assert executor.request.inputs == []
    assert executor.request.output.content_type == "audio/wav"
    assert executor.request.parameters["cfg_scale"] == 4.0
    assert executor.request.parameters["seed"] == 42
    assert executor.metadata == {"phase": "5", "provider": "breeze-tts2"}


def test_salad_breeze_provider_job_identity_changes_with_voice(tmp_path: Path) -> None:
    executor = FakeExecutor(_wav_bytes())
    provider = SaladBreezeSpeechProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
    )

    asyncio.run(
        provider.generate_speech(
            text="Same narration.",
            model=BREEZE_TTS2_MODEL_ID,
            voice="Voice A",
            instructions="Measured delivery.",
            speed=1.0,
            output_format="wav",
        )
    )
    first_job_id = executor.request.job_id

    asyncio.run(
        provider.generate_speech(
            text="Same narration.",
            model=BREEZE_TTS2_MODEL_ID,
            voice="Voice B",
            instructions="Measured delivery.",
            speed=1.0,
            output_format="wav",
        )
    )
    second_job_id = executor.request.job_id

    assert first_job_id != second_job_id


class FailingExecutor:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def execute(self, request, *, metadata):
        raise self.error


def test_salad_breeze_cancelled_job_is_not_fallback_eligible(tmp_path: Path) -> None:
    error = InferenceTransportFailedError(
        "user cancelled",
        status=QueueJobStatus.CANCELLED,
    )
    provider = SaladBreezeSpeechProvider(
        executor=FailingExecutor(error),  # type: ignore[arg-type]
        temp_dir=tmp_path,
    )

    with pytest.raises(InferenceTransportFailedError) as captured:
        asyncio.run(
            provider.generate_speech(
                text="Narration.",
                model=BREEZE_TTS2_MODEL_ID,
                voice="Voice A",
                instructions="Measured.",
                speed=1.0,
                output_format="wav",
            )
        )
    assert captured.value.status is QueueJobStatus.CANCELLED


def test_salad_breeze_failed_job_is_explicitly_fallback_eligible(tmp_path: Path) -> None:
    error = InferenceTransportFailedError(
        "worker failed",
        status=QueueJobStatus.FAILED,
    )
    provider = SaladBreezeSpeechProvider(
        executor=FailingExecutor(error),  # type: ignore[arg-type]
        temp_dir=tmp_path,
    )

    with pytest.raises(BreezeFallbackEligibleError) as captured:
        asyncio.run(
            provider.generate_speech(
                text="Narration.",
                model=BREEZE_TTS2_MODEL_ID,
                voice="Voice A",
                instructions="Measured.",
                speed=1.0,
                output_format="wav",
            )
        )
    assert captured.value.reason == "breeze_terminal_transport_failure"
