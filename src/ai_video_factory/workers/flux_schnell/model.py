from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact

FLUX_SCHNELL_REFERENCE_TASK = "image.flux1_schnell.reference"
FLUX_SCHNELL_KEYFRAME_TASK = "image.flux1_schnell.keyframe"
FLUX_SCHNELL_MODEL_ID = "black-forest-labs/FLUX.1-schnell"
FLUX_SCHNELL_GENERATION_PROFILE = "flux1-schnell-bnb4-v1"
_SUPPORTED_TASKS = frozenset({FLUX_SCHNELL_REFERENCE_TASK, FLUX_SCHNELL_KEYFRAME_TASK})


class FluxSchnellImageParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    model_id: str
    prompt: str = Field(min_length=1, max_length=100_000)
    width: int = Field(ge=256, le=2048)
    height: int = Field(ge=256, le=2048)
    seed: int = Field(ge=0, le=2_147_483_647)
    num_inference_steps: int = Field(default=4, ge=1, le=4)

    @model_validator(mode="after")
    def validate_flux(self) -> Self:
        if self.generation_profile != FLUX_SCHNELL_GENERATION_PROFILE:
            raise ValueError("Unexpected FLUX Schnell generation profile")
        if self.model_id != FLUX_SCHNELL_MODEL_ID:
            raise ValueError(f"FLUX worker requires model {FLUX_SCHNELL_MODEL_ID!r}")
        if self.width % 16 or self.height % 16:
            raise ValueError("FLUX width and height must be divisible by 16")
        return self


def flux_application_job_id(
    *,
    task_name: str,
    prompt: str,
    width: int,
    height: int,
    model_id: str = FLUX_SCHNELL_MODEL_ID,
) -> str:
    if task_name not in _SUPPORTED_TASKS:
        raise ValueError(f"Unsupported FLUX application task: {task_name}")
    purpose = "reference" if task_name == FLUX_SCHNELL_REFERENCE_TASK else "keyframe"
    payload = {
        "generation_profile": FLUX_SCHNELL_GENERATION_PROFILE,
        "height": height,
        "model_id": model_id,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "purpose": purpose,
        "width": width,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"flux-{purpose}-{hashlib.sha256(canonical).hexdigest()[:32]}"


def flux_seed_for_job(job_id: str) -> int:
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


class FluxSchnellBackend:
    def __init__(self, *, model_root: Path, device: str = "cuda") -> None:
        self._model_root = model_root
        self._device = device
        self._pipeline: Any | None = None
        self._torch: Any | None = None
        self._lock = threading.Lock()

    @property
    def bootstrap_marker(self) -> Path:
        return self._model_root / ".ready"

    @property
    def snapshot_root(self) -> Path:
        return self._model_root / "snapshot"

    def prepare(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            self._get_or_build_pipeline()

    def ready(self) -> None:
        with self._lock:
            self._validate_bootstrap()
            if self._pipeline is None:
                raise RuntimeError("FLUX Schnell runtime has not been prepared")

    def generate(self, *, parameters: FluxSchnellImageParameters, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        with self._lock:
            pipeline = self._get_or_build_pipeline()
            torch = self._torch
            if torch is None:
                raise RuntimeError("FLUX torch runtime is unavailable")
            generator = torch.Generator(device="cpu").manual_seed(parameters.seed)
            result = pipeline(
                prompt=parameters.prompt,
                width=parameters.width,
                height=parameters.height,
                guidance_scale=0.0,
                num_inference_steps=parameters.num_inference_steps,
                max_sequence_length=256,
                generator=generator,
            )
            images = result.images
            if len(images) != 1:
                raise RuntimeError("FLUX Schnell did not return exactly one image")
            image = images[0]
            if image.size != (parameters.width, parameters.height):
                raise RuntimeError("FLUX Schnell returned unexpected image dimensions")
            image.save(output_path, format="PNG")

    def _validate_bootstrap(self) -> None:
        if not self.bootstrap_marker.is_file() or not self.snapshot_root.is_dir():
            raise ModelBootstrapPendingError("FLUX Schnell model snapshot is not ready")

    def _get_or_build_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        try:
            import torch
            from diffusers import BitsAndBytesConfig as DiffusersBitsAndBytesConfig
            from diffusers import FluxPipeline, FluxTransformer2DModel
            from transformers import BitsAndBytesConfig as TransformersBitsAndBytesConfig
            from transformers import T5EncoderModel
        except ImportError as exc:
            raise RuntimeError("FLUX Schnell runtime dependencies are not installed") from exc

        if self._device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the FLUX Schnell runtime")

        snapshot = str(self.snapshot_root)
        transformer_quant = DiffusersBitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        text_quant = TransformersBitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        transformer = FluxTransformer2DModel.from_pretrained(
            snapshot,
            subfolder="transformer",
            quantization_config=transformer_quant,
            torch_dtype=torch.bfloat16,
            local_files_only=True,
        )
        text_encoder_2 = T5EncoderModel.from_pretrained(
            snapshot,
            subfolder="text_encoder_2",
            quantization_config=text_quant,
            torch_dtype=torch.bfloat16,
            local_files_only=True,
        )
        pipeline = FluxPipeline.from_pretrained(
            snapshot,
            transformer=transformer,
            text_encoder_2=text_encoder_2,
            torch_dtype=torch.bfloat16,
            local_files_only=True,
        )
        pipeline.enable_model_cpu_offload()
        self._torch = torch
        self._pipeline = pipeline
        return pipeline


class FluxSchnellImageTaskRunner:
    def __init__(self, *, backend: FluxSchnellBackend, task_name: str) -> None:
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
        if request.task != self.task_name:
            raise ValueError(f"FLUX runner cannot execute task {request.task!r}")
        if inputs or request.inputs:
            raise ValueError("FLUX fallback tasks do not accept object inputs")
        if request.output.content_type != "image/png":
            raise ValueError("FLUX output must be image/png")
        parameters = FluxSchnellImageParameters.model_validate(request.parameters)
        output = work_dir / "image.png"
        self._backend.generate(parameters=parameters, output_path=output)
        if not output.is_file() or output.stat().st_size <= 8:
            raise RuntimeError("FLUX Schnell produced no usable PNG output")
        return LocalArtifact(path=output, content_type="image/png")
