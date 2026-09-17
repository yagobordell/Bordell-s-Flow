from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact

FLUX_SCHNELL_REFERENCE_TASK = "image.flux_schnell.reference"
FLUX_SCHNELL_KEYFRAME_TASK = "image.flux_schnell.keyframe"
FLUX_SCHNELL_MODEL_ID = "black-forest-labs/FLUX.1-schnell"
FLUX_SCHNELL_GENERATION_PROFILE = "flux1-schnell-bnb4-nf4-4step-v1"

_SUPPORTED_TASKS = frozenset({FLUX_SCHNELL_REFERENCE_TASK, FLUX_SCHNELL_KEYFRAME_TASK})


class FluxImageParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    model_id: str
    prompt: str = Field(min_length=1, max_length=40_000)
    width: int = Field(ge=256, le=2048)
    height: int = Field(ge=256, le=2048)
    seed: int = Field(ge=0, le=2_147_483_647)

    @field_validator("generation_profile")
    @classmethod
    def validate_generation_profile(cls, value: str) -> str:
        if value != FLUX_SCHNELL_GENERATION_PROFILE:
            raise ValueError(
                "generation_profile must be exactly "
                f"{FLUX_SCHNELL_GENERATION_PROFILE!r}"
            )
        return value

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        if value != FLUX_SCHNELL_MODEL_ID:
            raise ValueError(f"FLUX worker requires model {FLUX_SCHNELL_MODEL_ID!r}")
        return value

    @model_validator(mode="after")
    def validate_dimensions(self) -> Self:
        if self.width % 16 or self.height % 16:
            raise ValueError("FLUX width and height must be divisible by 16")
        return self


class FluxBackend(Protocol):
    def prepare(self) -> None: ...

    def ready(self) -> None: ...

    def generate(self, *, parameters: FluxImageParameters, output_path: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class _FluxBindings:
    torch: Any
    pipeline_type: Any
    pipeline_quantization_config_type: Any


def _load_flux_bindings() -> _FluxBindings:
    try:
        import torch
        from diffusers import FluxPipeline
        from diffusers.quantizers import PipelineQuantizationConfig
    except ImportError as exc:
        raise RuntimeError("FLUX fallback runtime dependencies are not installed") from exc
    return _FluxBindings(
        torch=torch,
        pipeline_type=FluxPipeline,
        pipeline_quantization_config_type=PipelineQuantizationConfig,
    )


class FluxSchnellBackend:
    """Resident FLUX.1-schnell runtime with NF4 transformer/T5 quantization."""

    def __init__(
        self,
        *,
        model_root: Path,
        model_snapshot: Path,
        model_repository: str,
        model_revision: str,
        device: str = "cuda",
        inference_steps: int = 4,
        max_sequence_length: int = 256,
    ) -> None:
        self._model_root = model_root
        self._model_snapshot = model_snapshot
        self._model_repository = model_repository
        self._model_revision = model_revision
        self._device = device
        self._inference_steps = inference_steps
        self._max_sequence_length = max_sequence_length
        self._bindings: _FluxBindings | None = None
        self._pipeline: Any | None = None
        self._lock = threading.Lock()

    @property
    def bootstrap_marker(self) -> Path:
        return self._model_root / ".ready"

    def prepare(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            bindings = self._get_bindings()
            if self._device.startswith("cuda") and not bindings.torch.cuda.is_available():
                raise RuntimeError("CUDA is not available for the FLUX fallback runtime")
            self._get_or_build_pipeline(bindings)

    def ready(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            if self._pipeline is None:
                raise RuntimeError("FLUX fallback runtime has not been prepared")

    def generate(self, *, parameters: FluxImageParameters, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        with self._lock:
            bindings = self._get_bindings()
            pipeline = self._get_or_build_pipeline(bindings)
            generator = bindings.torch.Generator(device=self._device).manual_seed(parameters.seed)
            images = pipeline(
                prompt=parameters.prompt,
                height=parameters.height,
                width=parameters.width,
                guidance_scale=0.0,
                num_inference_steps=self._inference_steps,
                max_sequence_length=self._max_sequence_length,
                generator=generator,
            ).images
            if len(images) != 1:
                raise RuntimeError("FLUX fallback did not return exactly one image")
            image = images[0]
            if image.size != (parameters.width, parameters.height):
                raise RuntimeError("FLUX fallback returned an image with unexpected dimensions")
            image.save(output_path, format="PNG")

    def _get_bindings(self) -> _FluxBindings:
        if self._bindings is None:
            self._bindings = _load_flux_bindings()
        return self._bindings

    def _validate_bootstrap(self) -> None:
        if not self.bootstrap_marker.is_file():
            raise ModelBootstrapPendingError(
                f"FLUX model bootstrap marker is missing: {self.bootstrap_marker}"
            )
        marker = self.bootstrap_marker.read_text(encoding="utf-8").strip()
        expected = f"{self._model_repository}@{self._model_revision}"
        if marker != expected:
            raise RuntimeError(f"FLUX bootstrap marker {marker!r} does not match {expected!r}")
        if not self._model_snapshot.is_dir():
            raise ModelBootstrapPendingError(
                f"FLUX local model snapshot is missing: {self._model_snapshot}"
            )

    def _get_or_build_pipeline(self, bindings: _FluxBindings) -> Any:
        if self._pipeline is not None:
            return self._pipeline

        quantization_config = bindings.pipeline_quantization_config_type(
            quant_backend="bitsandbytes_4bit",
            quant_kwargs={
                "load_in_4bit": True,
                "bnb_4bit_quant_type": "nf4",
                "bnb_4bit_compute_dtype": bindings.torch.bfloat16,
            },
            components_to_quantize=["transformer", "text_encoder_2"],
        )
        pipeline = bindings.pipeline_type.from_pretrained(
            str(self._model_snapshot),
            quantization_config=quantization_config,
            torch_dtype=bindings.torch.bfloat16,
            local_files_only=True,
        )
        pipeline.to(self._device)
        self._pipeline = pipeline
        return pipeline


class FluxSchnellImageTaskRunner:
    def __init__(self, *, backend: FluxBackend, task_name: str) -> None:
        if task_name not in _SUPPORTED_TASKS:
            raise ValueError(f"Unsupported FLUX task runner: {task_name}")
        self._backend = backend
        self.task_name = task_name

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
        parameters = FluxImageParameters.model_validate(request.parameters)
        output = work_dir / "image.png"
        self._backend.generate(parameters=parameters, output_path=output)
        if not output.is_file() or output.stat().st_size <= 8:
            raise RuntimeError("FLUX fallback produced no usable PNG output")
        return LocalArtifact(path=output, content_type="image/png")

    def _validate_request(
        self,
        request: InferenceJobRequest,
        inputs: Mapping[str, Path],
    ) -> None:
        if request.task != self.task_name:
            raise ValueError(f"FLUX task runner cannot execute task {request.task!r}")
        if inputs or request.inputs:
            raise ValueError("FLUX text-to-image fallback does not accept object inputs")
        if request.output.content_type != "image/png":
            raise ValueError("FLUX fallback output must be image/png")
        if Path(request.output.key).suffix.lower() != ".png":
            raise ValueError("FLUX fallback output key must end in .png")
