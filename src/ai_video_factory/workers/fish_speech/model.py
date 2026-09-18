from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import wave
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact

FISH_SPEECH_TASK = "audio.fish_speech.generate"
FISH_SPEECH_MODEL_ID = "fishaudio/s2-pro"
FISH_SPEECH_MODEL_REVISION = "1de9996b6be38b745688de084d87a5633f714e4e"
FISH_SPEECH_RUNTIME_COMMIT = "214da3cd841bda85da2496b96cd3c4d7edb1337e"
FISH_SPEECH_GENERATION_PROFILE = "fish-s2-pro-bf16-v1"
FISH_SPEECH_CHUNKING_PROFILE = "sentence-utf8-v1"
FISH_SPEECH_OUTPUT_SAMPLE_RATE = 24_000
FISH_SPEECH_MAX_CHUNK_BYTES = 800
FISH_SPEECH_INTER_CHUNK_PAUSE_MS = 80


class FishSpeechParameters(BaseModel):
    """Strict immutable contract for one self-hosted Fish Speech generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    chunking_profile: str
    model_id: str
    model_revision: str
    runtime_commit: str
    text: str = Field(min_length=1, max_length=100_000)
    instructions: str = Field(default="", max_length=4_000)
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    reference_profile: str | None = Field(default=None, max_length=128)
    reference_audio_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reference_transcript: str | None = Field(default=None, min_length=1, max_length=20_000)
    reference_transcript_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_contract(self):
        expected = (
            (self.generation_profile, FISH_SPEECH_GENERATION_PROFILE, "generation_profile"),
            (self.chunking_profile, FISH_SPEECH_CHUNKING_PROFILE, "chunking_profile"),
            (self.model_id, FISH_SPEECH_MODEL_ID, "model_id"),
            (self.model_revision, FISH_SPEECH_MODEL_REVISION, "model_revision"),
            (self.runtime_commit, FISH_SPEECH_RUNTIME_COMMIT, "runtime_commit"),
        )
        for actual, wanted, field_name in expected:
            if actual != wanted:
                raise ValueError(f"{field_name} must be exactly {wanted!r}")

        reference_values = (
            self.reference_profile,
            self.reference_audio_sha256,
            self.reference_transcript,
            self.reference_transcript_sha256,
        )
        has_reference = any(value is not None for value in reference_values)
        if has_reference and not all(value is not None for value in reference_values):
            raise ValueError(
                "Fish reference conditioning requires profile, audio hash and transcript"
            )
        if self.reference_transcript is not None:
            transcript_hash = hashlib.sha256(
                self.reference_transcript.encode("utf-8")
            ).hexdigest()
            if transcript_hash != self.reference_transcript_sha256:
                raise ValueError("Fish reference transcript SHA-256 does not match transcript")
        return self


class FishBackend(Protocol):
    def prepare(self) -> None: ...

    def ready(self) -> None: ...

    def synthesize(
        self,
        *,
        parameters: FishSpeechParameters,
        reference_audio: Path | None,
        output_path: Path,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _FishBindings:
    torch: Any
    np: Any
    soundfile: Any
    model_manager_type: Any
    serve_reference_audio_type: Any
    serve_tts_request_type: Any


def _load_fish_bindings() -> _FishBindings:
    try:
        import numpy as np
        import soundfile
        import torch
        from fish_speech.utils.schema import ServeReferenceAudio, ServeTTSRequest
        from tools.server.model_manager import ModelManager
    except ImportError as exc:
        raise RuntimeError(
            "Fish Speech runtime dependencies are not installed in this environment"
        ) from exc
    return _FishBindings(
        torch=torch,
        np=np,
        soundfile=soundfile,
        model_manager_type=ModelManager,
        serve_reference_audio_type=ServeReferenceAudio,
        serve_tts_request_type=ServeTTSRequest,
    )


def fish_speech_application_job_id(
    *,
    text: str,
    instructions: str,
    speed: float,
    seed: int,
    reference_profile: str | None,
    reference_audio_sha256: str | None,
    reference_transcript: str | None,
    model_id: str = FISH_SPEECH_MODEL_ID,
    model_revision: str = FISH_SPEECH_MODEL_REVISION,
) -> str:
    transcript_sha = (
        hashlib.sha256(reference_transcript.encode("utf-8")).hexdigest()
        if reference_transcript is not None
        else None
    )
    payload = {
        "provider": "fish_speech",
        "model_id": model_id,
        "model_revision": model_revision,
        "runtime_commit": FISH_SPEECH_RUNTIME_COMMIT,
        "generation_profile": FISH_SPEECH_GENERATION_PROFILE,
        "chunking_profile": FISH_SPEECH_CHUNKING_PROFILE,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "reference_profile": reference_profile,
        "reference_audio_sha256": reference_audio_sha256,
        "reference_transcript_sha256": transcript_sha,
        "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        "speed": speed,
        "seed": seed,
        "output_format": "wav-pcm16-mono-24000",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"fish-speech-{hashlib.sha256(canonical).hexdigest()[:32]}"


class FishSpeechBackend:
    """Resident official Fish S2 Pro Python runtime producing canonical Phase 5 WAV."""

    def __init__(
        self,
        *,
        model_root: Path,
        model_repository: str = FISH_SPEECH_MODEL_ID,
        model_revision: str = FISH_SPEECH_MODEL_REVISION,
        runtime_commit: str = FISH_SPEECH_RUNTIME_COMMIT,
        device: str = "cuda",
        max_chunk_bytes: int = FISH_SPEECH_MAX_CHUNK_BYTES,
        inter_chunk_pause_ms: int = FISH_SPEECH_INTER_CHUNK_PAUSE_MS,
    ) -> None:
        if max_chunk_bytes < 200:
            raise ValueError("Fish max_chunk_bytes must be at least 200")
        if not 0 <= inter_chunk_pause_ms <= 2000:
            raise ValueError("Fish inter_chunk_pause_ms must be between 0 and 2000")
        self._model_root = model_root
        self._model_repository = model_repository
        self._model_revision = model_revision
        self._runtime_commit = runtime_commit
        self._device = device
        self._max_chunk_bytes = max_chunk_bytes
        self._inter_chunk_pause_ms = inter_chunk_pause_ms
        self._bindings: _FishBindings | None = None
        self._manager: Any | None = None
        self._lock = threading.Lock()

    @property
    def snapshot_root(self) -> Path:
        return self._model_root / "snapshot"

    @property
    def bootstrap_marker(self) -> Path:
        return self._model_root / ".ready"

    def prepare(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            self._get_or_build_manager()

    def ready(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            bindings = self._get_bindings()
            if self._manager is None:
                raise RuntimeError("Fish Speech runtime has not been prepared")
            if self._device.startswith("cuda") and not bindings.torch.cuda.is_available():
                raise RuntimeError("CUDA is not available for Fish Speech")

    def synthesize(
        self,
        *,
        parameters: FishSpeechParameters,
        reference_audio: Path | None,
        output_path: Path,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        raw_path = output_path.with_name(f"{output_path.stem}.native.wav")
        raw_path.unlink(missing_ok=True)

        with self._lock:
            manager = self._get_or_build_manager()
            bindings = self._get_bindings()
            chunks = split_narration_text(parameters.text, self._max_chunk_bytes)
            references: list[Any] = []
            if reference_audio is not None:
                if parameters.reference_transcript is None:
                    raise ValueError("Fish reference transcript is required with reference audio")
                references = [
                    bindings.serve_reference_audio_type(
                        audio=reference_audio.read_bytes(),
                        text=parameters.reference_transcript,
                    )
                ]

            is_cuda = self._device.startswith("cuda") and bindings.torch.cuda.is_available()
            if is_cuda:
                bindings.torch.cuda.reset_peak_memory_stats()
                bindings.torch.cuda.synchronize()
            started = time.monotonic()
            native_rate = int(manager.tts_inference_engine.decoder_model.sample_rate)
            pause_frames = round(native_rate * self._inter_chunk_pause_ms / 1000)
            generated: list[Any] = []
            for index, text_chunk in enumerate(chunks):
                request = bindings.serve_tts_request_type(
                    text=text_chunk,
                    references=references,
                    reference_id=None,
                    seed=(parameters.seed + index) & 0x7FFFFFFF,
                    use_memory_cache="on" if references else "off",
                    normalize=True,
                    streaming=False,
                    format="wav",
                    chunk_length=200,
                    max_new_tokens=1024,
                    top_p=0.8,
                    repetition_penalty=1.1,
                    temperature=0.8,
                )
                final_audio = None
                for result in manager.tts_inference_engine.inference(request):
                    if result.code == "error":
                        raise RuntimeError(f"Fish Speech inference failed: {result.error}")
                    if result.code == "final" and result.audio is not None:
                        result_rate, audio = result.audio
                        if int(result_rate) != native_rate:
                            raise RuntimeError(
                                "Fish Speech returned inconsistent native sample rate"
                            )
                        final_audio = audio
                if final_audio is None or len(final_audio) == 0:
                    raise RuntimeError("Fish Speech returned no audio for a narration chunk")
                generated.append(final_audio)
                if index + 1 < len(chunks) and pause_frames > 0:
                    generated.append(bindings.np.zeros(pause_frames, dtype="float32"))

            if not generated:
                raise RuntimeError("Fish Speech produced no audio")
            native_audio = bindings.np.concatenate(generated)
            bindings.soundfile.write(
                raw_path,
                native_audio,
                native_rate,
                subtype="PCM_16",
            )

            _normalize_wav(raw_path, output_path, parameters.speed)
            elapsed = time.monotonic() - started
            if is_cuda:
                bindings.torch.cuda.synchronize()
            audio_seconds = _validate_canonical_wav(output_path)
            peak_allocated = (
                bindings.torch.cuda.max_memory_allocated() if is_cuda else 0
            )
            peak_reserved = bindings.torch.cuda.max_memory_reserved() if is_cuda else 0
            rtf = elapsed / audio_seconds
            print(
                "FISH_SPEECH_INFERENCE_METRIC "
                f"elapsed_seconds={elapsed:.3f} text_bytes={len(parameters.text.encode('utf-8'))} "
                f"chunk_count={len(chunks)} audio_seconds={audio_seconds:.3f} "
                f"real_time_factor={rtf:.6f} peak_allocated_bytes={peak_allocated} "
                f"peak_reserved_bytes={peak_reserved}",
                flush=True,
            )

        raw_path.unlink(missing_ok=True)

    def _validate_bootstrap(self) -> None:
        if not self.bootstrap_marker.is_file() or not self.snapshot_root.is_dir():
            raise ModelBootstrapPendingError("Fish Speech model snapshot is not ready")
        expected = (
            f"{self._model_repository}@{self._model_revision}|"
            f"runtime@{self._runtime_commit}"
        )
        marker = self.bootstrap_marker.read_text(encoding="utf-8").strip()
        if marker != expected:
            raise ModelBootstrapPendingError(
                "Fish Speech bootstrap marker does not match pinned model/runtime revisions"
            )
        required = [
            self.snapshot_root / "config.json",
            self.snapshot_root / "model.safetensors.index.json",
            self.snapshot_root / "model-00001-of-00002.safetensors",
            self.snapshot_root / "model-00002-of-00002.safetensors",
            self.snapshot_root / "codec.pth",
            self.snapshot_root / "tokenizer.json",
        ]
        missing = [path.name for path in required if not path.is_file()]
        if missing:
            raise ModelBootstrapPendingError(
                "Fish Speech model snapshot is incomplete: " + ", ".join(missing)
            )

    def _get_bindings(self) -> _FishBindings:
        if self._bindings is None:
            self._bindings = _load_fish_bindings()
        return self._bindings

    def _get_or_build_manager(self) -> Any:
        if self._manager is not None:
            return self._manager
        bindings = self._get_bindings()
        if self._device.startswith("cuda") and not bindings.torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the Fish Speech production runtime")
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required by the Fish Speech worker")

        started = time.monotonic()
        manager = bindings.model_manager_type(
            mode="tts",
            device=self._device,
            half=False,
            compile=False,
            llama_checkpoint_path=str(self.snapshot_root),
            decoder_checkpoint_path=str(self.snapshot_root / "codec.pth"),
            decoder_config_name="modded_dac_vq",
        )
        elapsed = time.monotonic() - started
        allocated = (
            bindings.torch.cuda.memory_allocated()
            if self._device.startswith("cuda") and bindings.torch.cuda.is_available()
            else 0
        )
        reserved = (
            bindings.torch.cuda.memory_reserved()
            if self._device.startswith("cuda") and bindings.torch.cuda.is_available()
            else 0
        )
        print(
            "FISH_SPEECH_RUNTIME_READY "
            f"elapsed_seconds={elapsed:.3f} device={self._device} "
            f"allocated_bytes={allocated} reserved_bytes={reserved}",
            flush=True,
        )
        self._manager = manager
        return manager


class FishSpeechTaskRunner:
    task_name = FISH_SPEECH_TASK

    def __init__(self, *, backend: FishBackend) -> None:
        self._backend = backend

    def prepare(self) -> None:
        self._backend.prepare()

    def ready(self) -> None:
        self._backend.ready()

    def run(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
        work_dir: Path,
    ) -> LocalArtifact:
        if request.task != self.task_name:
            raise ValueError(f"FishSpeechTaskRunner cannot execute task {request.task!r}")
        if request.output.content_type != "audio/wav":
            raise ValueError("audio.fish_speech.generate output must be audio/wav")
        if Path(request.output.key).suffix.lower() != ".wav":
            raise ValueError("audio.fish_speech.generate output key must end in .wav")

        parameters = FishSpeechParameters.model_validate(request.parameters)
        has_reference = parameters.reference_profile is not None
        if has_reference:
            if set(inputs) != {"reference_audio"} or len(request.inputs) != 1:
                raise ValueError("conditioned Fish jobs require exactly reference_audio")
            reference_input = request.inputs[0]
            if reference_input.name != "reference_audio":
                raise ValueError("Fish reference input must be named reference_audio")
            if reference_input.sha256 != parameters.reference_audio_sha256:
                raise ValueError(
                    "Fish reference input SHA-256 must match the deterministic request identity"
                )
            if reference_input.content_type != "audio/wav":
                raise ValueError("Fish reference input must be audio/wav")
        elif inputs or request.inputs:
            raise ValueError("unconditioned Fish jobs do not accept object inputs")

        output = work_dir / "narration.wav"
        self._backend.synthesize(
            parameters=parameters,
            reference_audio=inputs.get("reference_audio"),
            output_path=output,
        )
        _validate_canonical_wav(output)
        return LocalArtifact(path=output, content_type="audio/wav")


def split_narration_text(text: str, max_bytes: int) -> list[str]:
    """Split without truncation, preferring sentence/whitespace boundaries."""

    if not text.strip():
        raise ValueError("Fish narration text must be non-empty")
    value = text
    if not value:
        raise ValueError("Fish narration text must be non-empty")
    if len(value.encode("utf-8")) <= max_bytes:
        return [value]

    chunks: list[str] = []
    start = 0
    while start < len(value):
        byte_count = 0
        end = start
        while end < len(value):
            encoded = value[end].encode("utf-8")
            if byte_count + len(encoded) > max_bytes:
                break
            byte_count += len(encoded)
            end += 1
        if end == len(value):
            chunks.append(value[start:end])
            break
        if end == start:
            end = start + 1

        window = value[start:end]
        sentence_matches = list(re.finditer(r"[.!?](?:\s+|$)", window))
        whitespace_matches = list(re.finditer(r"\s+", window))
        if sentence_matches:
            cut = start + sentence_matches[-1].end()
        elif whitespace_matches:
            cut = start + whitespace_matches[-1].end()
        else:
            cut = end
        if cut <= start:
            cut = end
        chunks.append(value[start:cut])
        start = cut

    if "".join(chunks) != value:
        raise AssertionError("Fish chunking must preserve every input character")
    return chunks


def _atempo_chain(speed: float) -> str:
    if not 0.25 <= speed <= 4.0:
        raise ValueError("Fish speech speed must be between 0.25 and 4.0")
    factors: list[float] = []
    remaining = speed
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={factor:.8g}" for factor in factors)


def _normalize_wav(source: Path, destination: Path, speed: float) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-filter:a",
        _atempo_chain(speed),
        "-ar",
        str(FISH_SPEECH_OUTPUT_SAMPLE_RATE),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown ffmpeg error"
        raise RuntimeError(f"Fish Speech WAV normalization failed: {detail}")


def _validate_canonical_wav(path: Path) -> float:
    if not path.is_file() or path.stat().st_size <= 44:
        raise RuntimeError("Fish Speech produced no usable WAV output")
    try:
        with wave.open(str(path), "rb") as wav_file:
            if wav_file.getframerate() != FISH_SPEECH_OUTPUT_SAMPLE_RATE:
                raise RuntimeError("Fish Speech WAV must be 24 kHz")
            if wav_file.getnchannels() != 1:
                raise RuntimeError("Fish Speech WAV must be mono")
            if wav_file.getsampwidth() != 2:
                raise RuntimeError("Fish Speech WAV must be PCM16")
            frame_count = wav_file.getnframes()
            if frame_count <= 0:
                raise RuntimeError("Fish Speech WAV must contain audio frames")
            frames = wav_file.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise RuntimeError("Fish Speech produced corrupt WAV output") from exc
    if not frames:
        raise RuntimeError("Fish Speech WAV contains no frame data")
    return frame_count / FISH_SPEECH_OUTPUT_SAMPLE_RATE
