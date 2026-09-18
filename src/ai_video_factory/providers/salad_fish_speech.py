from __future__ import annotations

import asyncio
import hashlib
import io
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    ObjectInput,
    ObjectOutput,
)
from ai_video_factory.workers.fish_speech import (
    FISH_SPEECH_CHUNKING_PROFILE,
    FISH_SPEECH_GENERATION_PROFILE,
    FISH_SPEECH_MODEL_ID,
    FISH_SPEECH_MODEL_REVISION,
    FISH_SPEECH_RUNTIME_COMMIT,
    FISH_SPEECH_TASK,
    fish_speech_application_job_id,
)

from .inference_jobs import InferenceJobExecutor
from .speech import GeneratedSpeech, SpeechFormat


@dataclass(frozen=True, slots=True)
class FishSpeechReference:
    """Authorized project-owned reference identity stored outside the Docker image."""

    profile: str
    audio_key: str
    audio_sha256: str
    transcript: str

    def __post_init__(self) -> None:
        if not self.profile.strip():
            raise ValueError("Fish reference profile must be non-empty")
        if not self.audio_key.strip():
            raise ValueError("Fish reference audio R2 key must be non-empty")
        if len(self.audio_sha256) != 64:
            raise ValueError("Fish reference audio SHA-256 must contain 64 hexadecimal characters")
        try:
            int(self.audio_sha256, 16)
        except ValueError as exc:
            raise ValueError("Fish reference audio SHA-256 must be hexadecimal") from exc
        if not self.transcript.strip():
            raise ValueError("Fish reference transcript must be non-empty")

    @property
    def transcript_sha256(self) -> str:
        return hashlib.sha256(self.transcript.encode("utf-8")).hexdigest()


class SaladFishSpeechProvider:
    """Self-hosted Fish Speech provider backed only by the dedicated Salad queue and R2."""

    def __init__(
        self,
        *,
        executor: InferenceJobExecutor,
        temp_dir: Path,
        reference: FishSpeechReference | None,
        seed: int = 42,
        allow_unconditioned: bool = False,
    ) -> None:
        if not 0 <= seed <= 2_147_483_647:
            raise ValueError("Fish seed is outside the supported range")
        if reference is None and not allow_unconditioned:
            raise ValueError(
                "Fish fallback requires an authorized reference voice; "
                "unconditioned generation is smoke-only"
            )
        self._executor = executor
        self._temp_dir = temp_dir
        self._reference = reference
        self._seed = seed
        self._allow_unconditioned = allow_unconditioned

    async def generate_speech(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        instructions: str,
        speed: float,
        output_format: SpeechFormat,
    ) -> GeneratedSpeech:
        return await asyncio.to_thread(
            self._generate_speech_sync,
            text=text,
            model=model,
            voice=voice,
            instructions=instructions,
            speed=speed,
            output_format=output_format,
        )

    def _generate_speech_sync(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        instructions: str,
        speed: float,
        output_format: SpeechFormat,
    ) -> GeneratedSpeech:
        if model != FISH_SPEECH_MODEL_ID:
            raise ValueError(f"Fish provider requires model {FISH_SPEECH_MODEL_ID!r}")
        if output_format != "wav":
            raise ValueError("Fish provider supports only canonical WAV output")
        if not text.strip():
            raise ValueError("Fish narration text must be non-empty")
        if not 0.25 <= speed <= 4.0:
            raise ValueError("Fish speech speed must be between 0.25 and 4.0")

        reference = self._reference
        if reference is None:
            if not self._allow_unconditioned:
                raise ValueError("Unconditioned Fish generation is not enabled")
            reference_profile = None
            reference_audio_sha256 = None
            reference_transcript = None
            reference_transcript_sha256 = None
            inputs: list[ObjectInput] = []
        else:
            if voice != reference.profile:
                raise ValueError(
                    "Fish voice must be the configured reference profile ID, "
                    "not a Breeze natural-language voice description"
                )
            reference_profile = reference.profile
            reference_audio_sha256 = reference.audio_sha256.lower()
            reference_transcript = reference.transcript
            reference_transcript_sha256 = reference.transcript_sha256
            inputs = [
                ObjectInput(
                    name="reference_audio",
                    key=reference.audio_key,
                    sha256=reference_audio_sha256,
                    content_type="audio/wav",
                )
            ]

        job_id = fish_speech_application_job_id(
            text=text,
            instructions=instructions,
            speed=speed,
            seed=self._seed,
            reference_profile=reference_profile,
            reference_audio_sha256=reference_audio_sha256,
            reference_transcript=reference_transcript,
            model_id=model,
        )
        request = InferenceJobRequest(
            job_id=job_id,
            task=FISH_SPEECH_TASK,
            inputs=inputs,
            output=ObjectOutput(
                key=f"jobs/{job_id}/narration.wav",
                content_type="audio/wav",
            ),
            parameters={
                "generation_profile": FISH_SPEECH_GENERATION_PROFILE,
                "chunking_profile": FISH_SPEECH_CHUNKING_PROFILE,
                "model_id": model,
                "model_revision": FISH_SPEECH_MODEL_REVISION,
                "runtime_commit": FISH_SPEECH_RUNTIME_COMMIT,
                "text": text,
                "instructions": instructions,
                "speed": speed,
                "seed": self._seed,
                "reference_profile": reference_profile,
                "reference_audio_sha256": reference_audio_sha256,
                "reference_transcript": reference_transcript,
                "reference_transcript_sha256": reference_transcript_sha256,
            },
        )
        response = self._executor.execute(
            request,
            metadata={
                "phase": "5",
                "provider": "fish_speech",
                "reference-profile": reference_profile or "unconditioned",
            },
        )

        self._temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._temp_dir) as directory:
            destination = Path(directory) / "narration.wav"
            self._executor.download_output(response, destination)
            content = destination.read_bytes()

        wav = _inspect_canonical_wav(content)
        return GeneratedSpeech(
            content=content,
            media_type="audio/wav",
            extension="wav",
            metadata={
                "provider": "fish_speech",
                "model": model,
                "model_revision": FISH_SPEECH_MODEL_REVISION,
                "runtime_commit": FISH_SPEECH_RUNTIME_COMMIT,
                "generation_profile": FISH_SPEECH_GENERATION_PROFILE,
                "chunking_profile": FISH_SPEECH_CHUNKING_PROFILE,
                "job_id": job_id,
                "request_sha256": response.request_sha256,
                "reference_profile": reference_profile or "unconditioned",
                "reference_audio_sha256": reference_audio_sha256 or "",
                "reference_transcript_sha256": reference_transcript_sha256 or "",
                "output_sha256": response.output.sha256,
                "duration_seconds": wav["duration_seconds"],
                "sample_rate": wav["sample_rate"],
                "channels": wav["channels"],
                "bit_depth": wav["bit_depth"],
                "replayed": response.replayed,
            },
        )


def _inspect_canonical_wav(content: bytes) -> dict[str, int | float]:
    if len(content) <= 44:
        raise ValueError("Fish worker returned an empty WAV artifact")
    try:
        with wave.open(io.BytesIO(content), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_count = wav_file.getnframes()
            frames = wav_file.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise ValueError("Fish worker returned invalid WAV data") from exc
    if sample_rate != 24_000 or channels != 1 or sample_width != 2:
        raise ValueError("Fish worker output must be PCM16 mono 24 kHz WAV")
    if frame_count <= 0 or not frames:
        raise ValueError("Fish worker returned an empty WAV")
    return {
        "duration_seconds": frame_count / sample_rate,
        "sample_rate": sample_rate,
        "channels": channels,
        "bit_depth": sample_width * 8,
    }
