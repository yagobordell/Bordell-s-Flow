from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact

BREEZE_TTS2_TASK = "audio.breeze_tts2.generate"
BREEZE_TTS2_MODEL_ID = "BreezeBlue/Breeze-TTS-2"
BREEZE_TTS2_GENERATION_PROFILE = "breeze-tts2-fast-decode-v4"

_MAX_NEW_TOKENS = 1500
_MAX_SEQ_LEN = 2048
_MAX_PROMPT_TOKENS = _MAX_SEQ_LEN - _MAX_NEW_TOKENS
# Reference-conditioned continuation adds the reference transcript and encoded audio
# to the prompt. Reserve room for those tokens before packing later chunks.
_REFERENCE_PROMPT_RESERVE_TOKENS = 192
_REPETITION_PENALTY = 1.1


class BreezeSpeechParameters(BaseModel):
    """Validated generation controls carried by one narration job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    model_id: str
    text: str = Field(min_length=1, max_length=50_000)
    voice: str = Field(min_length=1, max_length=4_000)
    instructions: str = Field(default="", max_length=4_000)
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    cfg_scale: float = Field(default=4.0, gt=0.0, le=10.0)
    seed: int = Field(default=42, ge=0, le=2_147_483_647)

    @field_validator("generation_profile")
    @classmethod
    def validate_generation_profile(cls, value: str) -> str:
        if value != BREEZE_TTS2_GENERATION_PROFILE:
            raise ValueError(
                "generation_profile must be exactly "
                f"{BREEZE_TTS2_GENERATION_PROFILE!r}"
            )
        return value

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        if value != BREEZE_TTS2_MODEL_ID:
            raise ValueError(f"model_id must be exactly {BREEZE_TTS2_MODEL_ID!r}")
        return value

    @field_validator("text", "voice", "instructions")
    @classmethod
    def normalize_text_fields(cls, value: str) -> str:
        return value.strip()


class BreezeBackend(Protocol):
    def prepare(self) -> None: ...

    def ready(self) -> None: ...

    def synthesize(
        self,
        *,
        parameters: BreezeSpeechParameters,
        output_path: Path,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _BreezeBindings:
    torch: Any
    np: Any
    soundfile: Any
    load_runtime: Any
    set_all_seeds: Any
    update_generation_config_for_breeze: Any
    get_template: Any
    prepare_inputs: Any
    select_template_name: Any
    fast_runtime_type: Any
    fast_config_type: Any
    load_warmup_profile: Any


def _load_breeze_bindings() -> _BreezeBindings:
    try:
        import numpy as np
        import soundfile
        import torch
        from breeze_infer.runtime import (
            load_runtime,
            set_all_seeds,
            update_generation_config_for_breeze,
        )
        from breeze_infer.templates import (
            get_template,
            prepare_inputs,
            select_template_name,
        )
        from models.fast_streaming import FastBreezeStreamingRuntime, FastStreamingConfig
        from models.warmup_profile import load_warmup_profile
    except ImportError as exc:
        raise RuntimeError(
            "Breeze TTS 2 runtime dependencies are not installed in this environment"
        ) from exc

    return _BreezeBindings(
        torch=torch,
        np=np,
        soundfile=soundfile,
        load_runtime=load_runtime,
        set_all_seeds=set_all_seeds,
        update_generation_config_for_breeze=update_generation_config_for_breeze,
        get_template=get_template,
        prepare_inputs=prepare_inputs,
        select_template_name=select_template_name,
        fast_runtime_type=FastBreezeStreamingRuntime,
        fast_config_type=FastStreamingConfig,
        load_warmup_profile=load_warmup_profile,
    )


def breeze_application_job_id(
    *,
    text: str,
    voice: str,
    instructions: str,
    speed: float,
    cfg_scale: float,
    seed: int,
    model_id: str = BREEZE_TTS2_MODEL_ID,
) -> str:
    payload = {
        "cfg_scale": cfg_scale,
        "generation_profile": BREEZE_TTS2_GENERATION_PROFILE,
        "instructions": instructions,
        "model_id": model_id,
        "seed": seed,
        "speed": speed,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "voice": voice,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"breeze-{hashlib.sha256(canonical).hexdigest()[:32]}"


class BreezeTTS2Backend:
    """Resident Breeze TTS 2 accelerated runtime producing canonical narration WAVs."""

    def __init__(
        self,
        *,
        model_root: Path,
        runtime_root: Path,
        model_repository: str = BREEZE_TTS2_MODEL_ID,
        model_revision: str = "main",
        device: str = "cuda",
        max_chunk_chars: int = 1200,
        inter_chunk_pause_ms: int = 120,
    ) -> None:
        if max_chunk_chars < 200:
            raise ValueError("Breeze max_chunk_chars must be at least 200")
        if not 0 <= inter_chunk_pause_ms <= 2000:
            raise ValueError("Breeze inter_chunk_pause_ms must be between 0 and 2000")
        self._model_root = model_root
        self._runtime_root = runtime_root
        self._model_repository = model_repository
        self._model_revision = model_revision
        self._device = device
        self._max_chunk_chars = max_chunk_chars
        self._inter_chunk_pause_ms = inter_chunk_pause_ms
        self._bindings: _BreezeBindings | None = None
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._audio_tokenizer: Any | None = None
        self._runtime: Any | None = None
        self._lock = threading.Lock()

    @property
    def runtime_loaded(self) -> bool:
        return self._runtime is not None

    @property
    def bootstrap_marker(self) -> Path:
        return self._model_root / ".ready"

    def prepare(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            self._get_or_build_runtime(bindings)

    def ready(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            if self._runtime is None:
                raise RuntimeError("Breeze TTS 2 runtime has not been prepared")

    def synthesize(
        self,
        *,
        parameters: BreezeSpeechParameters,
        output_path: Path,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path = output_path.with_name(f"{output_path.stem}.raw.wav")

        with self._lock:
            self._validate_bootstrap()
            bindings = self._get_bindings()
            self._validate_runtime(bindings)
            runtime = self._get_or_build_runtime(bindings)
            instruction = _compose_instruction(parameters.voice, parameters.instructions)
            if self._tokenizer is None:
                raise RuntimeError("Breeze TTS 2 tokenizer has not been prepared")
            chunks = _plan_narration_chunks(
                parameters.text,
                tokenizer=self._tokenizer,
                instruction=instruction,
                max_chunk_chars=self._max_chunk_chars,
            )
            pause_frames = round(runtime.sample_rate * self._inter_chunk_pause_ms / 1000)
            reference_path: Path | None = None
            reference_audio: list[Any] = []

            try:
                with bindings.soundfile.SoundFile(
                    raw_path,
                    mode="w",
                    samplerate=runtime.sample_rate,
                    channels=1,
                    subtype="PCM_16",
                ) as output_file:
                    for index, text_chunk in enumerate(chunks):
                        request_id = f"narration-{index:04d}"
                        request = {
                            "id": request_id,
                            "text": text_chunk,
                            "speaker": "S0",
                            "instruction": instruction,
                        }
                        if reference_path is not None:
                            request["ref_audio_path"] = str(reference_path)
                            request["ref_text"] = chunks[0]

                        template_name = bindings.select_template_name(request)
                        bindings.set_all_seeds(parameters.seed)
                        inputs = bindings.prepare_inputs(
                            self._tokenizer,
                            self._audio_tokenizer,
                            self._model,
                            [request],
                            bindings.get_template(template_name),
                            guidance_scale=parameters.cfg_scale,
                            guidance_scale_ref=None,
                            guidance_scale_ins=None,
                        )
                        for audio_chunk in runtime.iter_audio_chunks(
                            inputs,
                            request_id=request_id,
                            seed=parameters.seed,
                        ):
                            output_file.write(audio_chunk.audio)
                            if index == 0:
                                reference_audio.append(audio_chunk.audio.copy())

                        if index == 0 and len(chunks) > 1:
                            if not reference_audio:
                                raise RuntimeError(
                                    "Breeze TTS 2 produced no audio for the voice anchor"
                                )
                            reference_path = raw_path.with_name(
                                f"{raw_path.stem}.voice-reference.wav"
                            )
                            reference_samples = bindings.np.concatenate(reference_audio)
                            with bindings.soundfile.SoundFile(
                                reference_path,
                                mode="w",
                                samplerate=runtime.sample_rate,
                                channels=1,
                                subtype="PCM_16",
                            ) as reference_file:
                                reference_file.write(reference_samples)

                        if index + 1 < len(chunks) and pause_frames > 0:
                            output_file.write(bindings.np.zeros(pause_frames, dtype="float32"))
            finally:
                if reference_path is not None:
                    reference_path.unlink(missing_ok=True)

        try:
            if math.isclose(parameters.speed, 1.0, rel_tol=0.0, abs_tol=1e-9):
                raw_path.replace(output_path)
            else:
                _apply_speed(raw_path, output_path, parameters.speed)
        finally:
            raw_path.unlink(missing_ok=True)

    def _get_bindings(self) -> _BreezeBindings:
        if self._bindings is None:
            self._bindings = _load_breeze_bindings()
        return self._bindings

    def _validate_bootstrap(self) -> None:
        if not self.bootstrap_marker.is_file():
            raise ModelBootstrapPendingError(
                f"Breeze model bootstrap marker is missing: {self.bootstrap_marker}"
            )
        marker = self.bootstrap_marker.read_text(encoding="utf-8").strip()
        expected = f"{self._model_repository}@{self._model_revision}"
        if marker != expected:
            raise RuntimeError(
                f"Breeze bootstrap marker {marker!r} does not match {expected!r}"
            )

    def _validate_runtime(self, bindings: _BreezeBindings) -> None:
        if not self._model_root.is_dir():
            raise RuntimeError(f"Breeze model directory does not exist: {self._model_root}")
        if not (self._model_root / "config.json").is_file():
            raise RuntimeError(f"Breeze config.json is missing from {self._model_root}")
        fast_config = self._runtime_root / "configs" / "fast.json"
        if not fast_config.is_file():
            raise FileNotFoundError(f"Breeze fast config is missing: {fast_config}")
        if self._device.startswith("cuda") and not bindings.torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the Breeze TTS 2 production runtime")
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required by the Breeze TTS 2 worker")

    def _get_or_build_runtime(self, bindings: _BreezeBindings) -> Any:
        if self._runtime is not None:
            return self._runtime

        tokenizer, model, audio_tokenizer = bindings.load_runtime(
            self._model_root,
            device=self._device,
            attn_implementation="eager",
        )
        bindings.update_generation_config_for_breeze(model)
        config = bindings.fast_config_type(
            max_new_tokens=_MAX_NEW_TOKENS,
            max_seq_len=_MAX_SEQ_LEN,
            # Reference-conditioned continuation adds a variable-length audio/text
            # prefix. The official CUDA-graph prefill cache is frozen to the finite
            # shapes in fast.json, so using it here makes valid reference requests
            # fail when their prefix falls outside that profile. Keep the static
            # decode/decoder/codec paths accelerated and deliberately use eager
            # prefill for every request shape.
            fast_all=False,
            fast_text_encoder=True,
            fast_backbone_prefill=False,
            fast_backbone_decode=True,
            fast_depth_decoder=True,
            fast_codec=True,
            repetition_penalty=_REPETITION_PENALTY,
        )
        runtime = bindings.fast_runtime_type(
            model,
            audio_tokenizer,
            config,
            tokenizer=tokenizer,
        )
        if not runtime.fast_enabled:
            raise RuntimeError("Breeze TTS 2 runtime did not enable an accelerated path")

        profile = bindings.load_warmup_profile(self._runtime_root / "configs" / "fast.json")
        profile = replace(profile, codec_chunk_frames=runtime.codec_chunk_frames)
        runtime.warmup_from_profile(profile)

        self._tokenizer = tokenizer
        self._model = model
        self._audio_tokenizer = audio_tokenizer
        self._runtime = runtime
        return runtime


class BreezeSpeechTaskRunner:
    task_name = BREEZE_TTS2_TASK

    def __init__(self, *, backend: BreezeBackend) -> None:
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
        parameters = BreezeSpeechParameters.model_validate(request.parameters)
        output = work_dir / "narration.wav"
        self._backend.synthesize(parameters=parameters, output_path=output)
        if not output.is_file() or output.stat().st_size <= 44:
            raise RuntimeError("Breeze TTS 2 produced no usable WAV output")
        return LocalArtifact(path=output, content_type="audio/wav")

    def _validate_request(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
    ) -> None:
        if request.task != self.task_name:
            raise ValueError(f"BreezeSpeechTaskRunner cannot execute task {request.task!r}")
        if inputs or request.inputs:
            raise ValueError("audio.breeze_tts2.generate does not accept object inputs")
        if request.output.content_type != "audio/wav":
            raise ValueError("audio.breeze_tts2.generate output must be audio/wav")
        if Path(request.output.key).suffix.lower() != ".wav":
            raise ValueError("audio.breeze_tts2.generate output key must end in .wav")


def _compose_instruction(voice: str, delivery: str) -> str:
    voice_text = voice.strip()
    delivery_text = delivery.strip()
    if delivery_text:
        return (
            f"Voice identity: {voice_text}. Delivery direction: {delivery_text}. "
            "Keep the same speaker identity throughout the narration."
        )
    return f"Voice identity: {voice_text}. Keep the same speaker identity throughout the narration."


def _split_narration_text(text: str, max_chars: int) -> list[str]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Breeze narration text must be non-empty")
    if len(stripped) <= max_chars:
        return [stripped]

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", stripped) if part.strip()]
    pieces: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        pieces.extend(_split_oversized_piece(sentence, max_chars))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = piece if not current else f"{current} {piece}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = piece
    if current:
        chunks.append(current)
    return chunks


def _prompt_token_count(tokenizer: Any, *, instruction: str, text: str) -> int:
    rendered = f"[S0]<ins_bos>{instruction}<ins_eos>{text}"
    encoded = tokenizer(rendered, add_special_tokens=True)
    token_ids = encoded["input_ids"]
    if token_ids and isinstance(token_ids[0], list):
        token_ids = token_ids[0]
    return len(token_ids)


def _fits_prompt(
    tokenizer: Any,
    *,
    instruction: str,
    text: str,
    token_budget: int,
    max_chars: int,
) -> bool:
    return len(text) <= max_chars and _prompt_token_count(
        tokenizer,
        instruction=instruction,
        text=text,
    ) <= token_budget


def _split_piece_for_prompt_budget(
    text: str,
    *,
    tokenizer: Any,
    instruction: str,
    token_budget: int,
    max_chars: int,
) -> list[str]:
    if _fits_prompt(
        tokenizer,
        instruction=instruction,
        text=text,
        token_budget=token_budget,
        max_chars=max_chars,
    ):
        return [text]

    words = text.split()
    if len(words) <= 1:
        raise ValueError(
            "Breeze narration contains a token that cannot fit within the runtime prompt budget"
        )

    midpoint = len(words) // 2
    left = " ".join(words[:midpoint])
    right = " ".join(words[midpoint:])
    return [
        *_split_piece_for_prompt_budget(
            left,
            tokenizer=tokenizer,
            instruction=instruction,
            token_budget=token_budget,
            max_chars=max_chars,
        ),
        *_split_piece_for_prompt_budget(
            right,
            tokenizer=tokenizer,
            instruction=instruction,
            token_budget=token_budget,
            max_chars=max_chars,
        ),
    ]


def _pack_prompt_pieces(
    pieces: list[str],
    *,
    tokenizer: Any,
    instruction: str,
    token_budget: int,
    max_chars: int,
) -> list[str]:
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        for fitting_piece in _split_piece_for_prompt_budget(
            piece,
            tokenizer=tokenizer,
            instruction=instruction,
            token_budget=token_budget,
            max_chars=max_chars,
        ):
            candidate = fitting_piece if not current else f"{current} {fitting_piece}"
            if current and not _fits_prompt(
                tokenizer,
                instruction=instruction,
                text=candidate,
                token_budget=token_budget,
                max_chars=max_chars,
            ):
                chunks.append(current)
                current = fitting_piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def _plan_narration_chunks(
    text: str,
    *,
    tokenizer: Any,
    instruction: str,
    max_chunk_chars: int,
) -> list[str]:
    """Pack narration to the runtime budget and anchor continuation voice identity.

    The runtime has a finite audio-token budget as well as a text-context budget. A request that
    fits the text context can still be truncated at the audio-token ceiling, so the configured
    chunk guard remains active for production narration. Splitting is sentence-aware and later
    chunks use a reference recording from the first chunk, so the guard cannot reset voice design.
    """

    stripped = text.strip()
    if not stripped:
        raise ValueError("Breeze narration text must be non-empty")
    if _fits_prompt(
        tokenizer,
        instruction=instruction,
        text=stripped,
        token_budget=_MAX_PROMPT_TOKENS,
        max_chars=max_chunk_chars,
    ):
        return [stripped]

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", stripped) if part.strip()]
    pieces: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chunk_chars:
            pieces.append(sentence)
        else:
            pieces.extend(_split_oversized_piece(sentence, max_chunk_chars))

    safe_pieces: list[str] = []
    for piece in pieces:
        safe_pieces.extend(
            _split_piece_for_prompt_budget(
                piece,
                tokenizer=tokenizer,
                instruction=instruction,
                token_budget=_MAX_PROMPT_TOKENS,
                max_chars=max_chunk_chars,
            )
        )

    first_chunk = safe_pieces[0]
    remainder_index = 1
    while remainder_index < len(safe_pieces):
        candidate = f"{first_chunk} {safe_pieces[remainder_index]}"
        if not _fits_prompt(
            tokenizer,
            instruction=instruction,
            text=candidate,
            token_budget=_MAX_PROMPT_TOKENS,
            max_chars=max_chunk_chars,
        ):
            break
        first_chunk = candidate
        remainder_index += 1

    remaining = [first_chunk]
    remainder_pieces = safe_pieces[remainder_index:]
    remaining.extend(
        _pack_prompt_pieces(
            remainder_pieces,
            tokenizer=tokenizer,
            instruction=instruction,
            token_budget=_MAX_PROMPT_TOKENS - _REFERENCE_PROMPT_RESERVE_TOKENS,
            max_chars=max_chunk_chars,
        )
    )
    return remaining


def _split_oversized_piece(text: str, max_chars: int) -> list[str]:
    words = text.split()
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(word) > max_chars:
            chunks.extend(
                word[index : index + max_chars]
                for index in range(0, len(word), max_chars)
            )
            current = ""
        else:
            current = word
    if current:
        chunks.append(current)
    return chunks


def _atempo_chain(speed: float) -> str:
    if not 0.25 <= speed <= 4.0:
        raise ValueError("Breeze speech speed must be between 0.25 and 4.0")
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


def _apply_speed(source: Path, destination: Path, speed: float) -> None:
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
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown ffmpeg error"
        raise RuntimeError(f"Breeze narration speed adjustment failed: {detail}")
