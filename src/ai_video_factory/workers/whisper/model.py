from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.ports import LocalArtifact

WHISPER_TRANSCRIPTION_TASK = "audio.whisper.transcribe"
WHISPER_MODEL_ID = "openai/whisper-large-v3-turbo"
WHISPER_GENERATION_PROFILE = "whisper-large-v3-turbo-fp16-v1"


class WhisperTranscriptionParameters(BaseModel):
    """Validated parameters carried by one Whisper transcription inference job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    model_id: str
    prompt: str = Field(default="", max_length=16000)
    language: str | None = Field(default=None, min_length=2, max_length=64)

    @field_validator("generation_profile")
    @classmethod
    def validate_generation_profile(cls, value: str) -> str:
        if value != WHISPER_GENERATION_PROFILE:
            raise ValueError(
                f"generation_profile must be exactly {WHISPER_GENERATION_PROFILE!r}"
            )
        return value

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        if value != WHISPER_MODEL_ID:
            raise ValueError(f"model_id must be exactly {WHISPER_MODEL_ID!r}")
        return value

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        return value.strip()

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized


class WhisperWord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)


class WhisperTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    model_id: str
    words: list[WhisperWord] = Field(min_length=1)


class WhisperBackend(Protocol):
    def prepare(self) -> None: ...

    def ready(self) -> None: ...

    def transcribe(
        self,
        *,
        audio_path: Path,
        parameters: WhisperTranscriptionParameters,
    ) -> list[WhisperWord]: ...


@dataclass(frozen=True, slots=True)
class _WhisperBindings:
    torch: Any
    pipeline_factory: Any


def _load_whisper_bindings() -> _WhisperBindings:
    try:
        import torch
        from transformers import pipeline
    except ImportError as exc:
        raise RuntimeError(
            "Whisper runtime dependencies are not installed in this environment"
        ) from exc
    return _WhisperBindings(torch=torch, pipeline_factory=pipeline)


def whisper_application_job_id(
    *,
    audio_sha256: str,
    prompt: str,
    language: str | None,
    model_id: str = WHISPER_MODEL_ID,
) -> str:
    payload = {
        "audio_sha256": audio_sha256,
        "generation_profile": WHISPER_GENERATION_PROFILE,
        "language": language,
        "model_id": model_id,
        "prompt": prompt,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"whisper-{hashlib.sha256(canonical).hexdigest()[:32]}"


class TransformersWhisperBackend:
    """Resident Transformers pipeline for Whisper Large V3 Turbo word timestamps."""

    def __init__(
        self,
        *,
        model_root: Path,
        device: str = "cuda:0",
        dtype: str = "float16",
    ) -> None:
        self._model_root = model_root
        self._device = device
        self._dtype = dtype
        self._pipeline: Any | None = None
        self._bindings: _WhisperBindings | None = None
        self._lock = threading.Lock()

    @property
    def pipeline_loaded(self) -> bool:
        return self._pipeline is not None

    def prepare(self) -> None:
        with self._lock:
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            self._get_or_build_pipeline(bindings)

    def ready(self) -> None:
        with self._lock:
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            if self._pipeline is None:
                raise RuntimeError("Whisper pipeline has not been prepared")

    def transcribe(
        self,
        *,
        audio_path: Path,
        parameters: WhisperTranscriptionParameters,
    ) -> list[WhisperWord]:
        if not audio_path.is_file():
            raise FileNotFoundError(f"Whisper audio input does not exist: {audio_path}")

        with self._lock:
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            pipeline = self._get_or_build_pipeline(bindings)
            generate_kwargs: dict[str, Any] = {"task": "transcribe"}
            if parameters.language is not None:
                generate_kwargs["language"] = parameters.language
            if parameters.prompt:
                prompt_ids = pipeline.tokenizer.get_prompt_ids(
                    parameters.prompt,
                    return_tensors="pt",
                )
                to_method = getattr(prompt_ids, "to", None)
                if callable(to_method):
                    prompt_ids = to_method(self._device)
                generate_kwargs["prompt_ids"] = prompt_ids

            result = pipeline(
                str(audio_path),
                return_timestamps="word",
                generate_kwargs=generate_kwargs,
            )

        raw_chunks = result.get("chunks") if isinstance(result, dict) else None
        if not isinstance(raw_chunks, list) or not raw_chunks:
            raise RuntimeError("Whisper returned no word timestamp chunks")

        words: list[WhisperWord] = []
        for chunk in raw_chunks:
            if not isinstance(chunk, Mapping):
                raise RuntimeError("Whisper returned an invalid timestamp chunk")
            text = chunk.get("text")
            timestamp = chunk.get("timestamp")
            if not isinstance(text, str) or not text.strip():
                continue
            if (
                not isinstance(timestamp, (list, tuple))
                or len(timestamp) != 2
                or not isinstance(timestamp[0], (int, float))
                or not isinstance(timestamp[1], (int, float))
            ):
                raise RuntimeError("Whisper returned invalid word timestamps")
            start = float(timestamp[0])
            end = float(timestamp[1])
            if end < start:
                raise RuntimeError("Whisper returned a word ending before it starts")
            words.append(
                WhisperWord(
                    text=text.strip(),
                    start_seconds=start,
                    end_seconds=end,
                )
            )

        if not words:
            raise RuntimeError("Whisper returned no usable word timestamps")
        return words

    def _get_bindings(self) -> _WhisperBindings:
        if self._bindings is None:
            self._bindings = _load_whisper_bindings()
        return self._bindings

    def _validate_runtime(self, bindings: _WhisperBindings) -> None:
        if not self._model_root.is_dir():
            raise FileNotFoundError(f"Whisper model directory does not exist: {self._model_root}")
        if not (self._model_root / "config.json").is_file():
            raise FileNotFoundError(f"Whisper config.json is missing from {self._model_root}")
        if self._device.startswith("cuda") and not bindings.torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the Whisper production runtime")
        if not hasattr(bindings.torch, self._dtype):
            raise ValueError(f"Unsupported Whisper torch dtype: {self._dtype}")

    def _get_or_build_pipeline(self, bindings: _WhisperBindings) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        torch_dtype = getattr(bindings.torch, self._dtype)
        self._pipeline = bindings.pipeline_factory(
            task="automatic-speech-recognition",
            model=str(self._model_root),
            dtype=torch_dtype,
            device=self._device,
        )
        return self._pipeline


class WhisperTaskRunner:
    """Inference task adapter producing provider-neutral word timestamp JSON."""

    task_name = WHISPER_TRANSCRIPTION_TASK

    def __init__(self, *, backend: WhisperBackend) -> None:
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
        self._validate_request(request, inputs)
        parameters = WhisperTranscriptionParameters.model_validate(request.parameters)
        words = self._backend.transcribe(
            audio_path=inputs["audio"],
            parameters=parameters,
        )
        transcript = WhisperTranscript(model_id=parameters.model_id, words=words)
        work_dir.mkdir(parents=True, exist_ok=True)
        output = work_dir / "words.json"
        output.write_text(
            transcript.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return LocalArtifact(path=output, content_type="application/json")

    def _validate_request(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
    ) -> None:
        if request.task != self.task_name:
            raise ValueError(f"WhisperTaskRunner cannot execute task {request.task!r}")
        if set(inputs) != {"audio"}:
            raise ValueError("audio.whisper.transcribe requires exactly one 'audio' input")
        declared_names = {item.name for item in request.inputs}
        if declared_names != {"audio"}:
            raise ValueError("audio.whisper.transcribe must declare one 'audio' input")
        if request.output.content_type != "application/json":
            raise ValueError("audio.whisper.transcribe output must be application/json")
        if Path(request.output.key).suffix.lower() != ".json":
            raise ValueError("audio.whisper.transcribe output key must end in .json")
