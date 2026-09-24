from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_video_factory.image_contracts import (
    QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    QWEN_IMAGE_21_PRODUCTION_SIZE,
    QWEN_IMAGE_21_PRODUCTION_WIDTH,
)
from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact, LocalSidecarArtifact

QWEN_IMAGE_21_REFERENCE_TASK = "image.qwen_image_21.reference"
QWEN_IMAGE_21_KEYFRAME_TASK = "image.qwen_image_21.keyframe"
QWEN_IMAGE_21_MODEL_ID = "Qwen/Qwen-Image-2.1"
QWEN_IMAGE_21_MODEL_REVISION = "b3179ad355be050328e483a9dfdd9e60cd62adfa"
QWEN_IMAGE_21_DIMENSION_MULTIPLE = 32
QWEN_IMAGE_21_DEFAULT_STEPS = 40
QWEN_IMAGE_21_TRUE_CFG_SCALE = 1.0
QWEN_IMAGE_21_USE_KV_CACHE = True
QWEN_IMAGE_21_GENERATION_PROFILE = "qwen-image-2.1-1280x736-40step-kv-v3"
QWEN_IMAGE_21_BENCHMARK_PROFILE = "qwen-image-2.1-1536x864-40step-kv-benchmark-v1"
QWEN_IMAGE_21_REQUIRED_MODEL_FILES = (
    "model_index.json",
    "processor/tokenizer.json",
    "scheduler/scheduler_config.json",
    "text_encoder/model.safetensors.index.json",
    "transformer/diffusion_pytorch_model.safetensors.index.json",
    "vae/diffusion_pytorch_model.safetensors",
)
_SUPPORTED_TASKS = frozenset({QWEN_IMAGE_21_REFERENCE_TASK, QWEN_IMAGE_21_KEYFRAME_TASK})


class QwenImage21Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    model_id: str
    model_revision: str
    prompt: str = Field(min_length=1, max_length=100_000)
    width: int = Field(ge=256, le=4096)
    height: int = Field(ge=256, le=4096)
    seed: int = Field(ge=0, le=2_147_483_647)
    num_inference_steps: int = Field(default=QWEN_IMAGE_21_DEFAULT_STEPS, ge=1, le=100)
    true_cfg_scale: float = Field(default=QWEN_IMAGE_21_TRUE_CFG_SCALE, ge=1.0)
    use_kv_cache: bool = QWEN_IMAGE_21_USE_KV_CACHE

    @model_validator(mode="after")
    def validate_qwen_image_21(self) -> Self:
        if self.generation_profile not in (
            QWEN_IMAGE_21_GENERATION_PROFILE,
            QWEN_IMAGE_21_BENCHMARK_PROFILE,
        ):
            raise ValueError("Unexpected Qwen-Image-2.1 generation profile")
        if self.model_id != QWEN_IMAGE_21_MODEL_ID:
            raise ValueError(f"Qwen worker requires model {QWEN_IMAGE_21_MODEL_ID!r}")
        if self.model_revision != QWEN_IMAGE_21_MODEL_REVISION:
            raise ValueError(
                f"Qwen worker requires revision {QWEN_IMAGE_21_MODEL_REVISION!r}"
            )
        if (
            self.width % QWEN_IMAGE_21_DIMENSION_MULTIPLE
            or self.height % QWEN_IMAGE_21_DIMENSION_MULTIPLE
        ):
            raise ValueError(
                f"Qwen-Image-2.1 width and height must be divisible by "
                f"{QWEN_IMAGE_21_DIMENSION_MULTIPLE}"
            )
        if self.generation_profile == QWEN_IMAGE_21_BENCHMARK_PROFILE:
            if (self.width, self.height) != (1536, 864):
                raise ValueError("Qwen-Image-2.1 benchmark requires 1536x864")
        elif (self.width, self.height) != (
            QWEN_IMAGE_21_PRODUCTION_WIDTH,
            QWEN_IMAGE_21_PRODUCTION_HEIGHT,
        ):
            raise ValueError(
                "Qwen-Image-2.1 production generation is fixed at "
                f"{QWEN_IMAGE_21_PRODUCTION_SIZE}"
            )
        if self.num_inference_steps != QWEN_IMAGE_21_DEFAULT_STEPS:
            raise ValueError(
                f"Qwen-Image-2.1 production generation requires "
                f"{QWEN_IMAGE_21_DEFAULT_STEPS} inference steps"
            )
        if self.true_cfg_scale != QWEN_IMAGE_21_TRUE_CFG_SCALE:
            raise ValueError("Qwen-Image-2.1 production generation uses guidance-free sampling")
        if self.use_kv_cache is not QWEN_IMAGE_21_USE_KV_CACHE:
            raise ValueError("Qwen-Image-2.1 production generation requires KV caching")
        return self


def qwen_image_21_application_job_id(
    *,
    task_name: str,
    prompt: str,
    width: int,
    height: int,
    model_id: str = QWEN_IMAGE_21_MODEL_ID,
    model_revision: str = QWEN_IMAGE_21_MODEL_REVISION,
) -> str:
    if task_name not in _SUPPORTED_TASKS:
        raise ValueError(f"Unsupported Qwen-Image-2.1 application task: {task_name}")
    purpose = "reference" if task_name == QWEN_IMAGE_21_REFERENCE_TASK else "keyframe"
    payload = {
        "generation_profile": QWEN_IMAGE_21_GENERATION_PROFILE,
        "height": height,
        "model_id": model_id,
        "model_revision": model_revision,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "purpose": purpose,
        "width": width,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"qwen-image-21-{purpose}-{hashlib.sha256(canonical).hexdigest()[:32]}"


def qwen_image_21_seed_for_job(job_id: str) -> int:
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


class QwenImage21Backend:
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
                raise RuntimeError("Qwen-Image-2.1 runtime has not been prepared")

    def generate(self, *, parameters: QwenImage21Parameters, output_path: Path) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        with self._lock:
            pipeline_reused = self._pipeline is not None
            pipeline = self._get_or_build_pipeline()
            torch = self._torch
            if torch is None:
                raise RuntimeError("Qwen-Image-2.1 torch runtime is unavailable")
            generator = torch.Generator(device=self._device).manual_seed(parameters.seed)
            is_cuda = self._device.startswith("cuda") and torch.cuda.is_available()
            if is_cuda:
                torch.cuda.reset_peak_memory_stats()
            started = time.monotonic()
            result = None
            try:
                result = pipeline(
                    prompt=parameters.prompt,
                    width=parameters.width,
                    height=parameters.height,
                    num_inference_steps=parameters.num_inference_steps,
                    true_cfg_scale=parameters.true_cfg_scale,
                    use_kv_cache=parameters.use_kv_cache,
                    output_type="pil",
                    generator=generator,
                )
                if is_cuda:
                    torch.cuda.synchronize()
                inference_seconds = time.monotonic() - started
                images = result.images
                if len(images) != 1:
                    raise RuntimeError("Qwen-Image-2.1 did not return exactly one image")
                image = images[0]
                if image.size != (parameters.width, parameters.height):
                    raise RuntimeError("Qwen-Image-2.1 returned unexpected image dimensions")
                save_started = time.monotonic()
                image.save(output_path, format="PNG")
                png_save_seconds = time.monotonic() - save_started
                elapsed = time.monotonic() - started
                peak_allocated = torch.cuda.max_memory_allocated() if is_cuda else 0
                peak_reserved = torch.cuda.max_memory_reserved() if is_cuda else 0
                print(
                    "QWEN_IMAGE_21_INFERENCE_METRIC "
                    f"elapsed_seconds={elapsed:.3f} inference_seconds={inference_seconds:.3f} "
                    f"encode_seconds={png_save_seconds:.3f} seed={parameters.seed} "
                    f"width={parameters.width} height={parameters.height} "
                    f"steps={parameters.num_inference_steps} "
                    f"peak_allocated_bytes={peak_allocated} peak_reserved_bytes={peak_reserved}",
                    flush=True,
                )
                return {
                    "schema_version": "1",
                    "generation_profile": parameters.generation_profile,
                    "model_revision": parameters.model_revision,
                    "width": parameters.width,
                    "height": parameters.height,
                    "num_inference_steps": parameters.num_inference_steps,
                    "seed": parameters.seed,
                    "memory_mode": os.environ.get("QWEN_IMAGE_21_MEMORY_MODE", "int8_cuda"),
                    "worker_id": f"{socket.gethostname()}-{os.getpid()}",
                    "pipeline_reused": pipeline_reused,
                    "inference_seconds": inference_seconds,
                    "png_save_seconds": png_save_seconds,
                    "total_elapsed_seconds": elapsed,
                    "peak_vram_allocated_bytes": peak_allocated,
                    "peak_vram_reserved_bytes": peak_reserved,
                }
            finally:
                del result
                # Keep the CUDA caching allocator warm between requests.
                # Releasing it after every image forces subsequent allocations.

    def _validate_bootstrap(self) -> None:
        expected = f"{QWEN_IMAGE_21_MODEL_ID}@{QWEN_IMAGE_21_MODEL_REVISION}"
        if not self.bootstrap_marker.is_file() or not self.snapshot_root.is_dir():
            raise ModelBootstrapPendingError("Qwen-Image-2.1 model snapshot is not ready")
        missing = [
            relative
            for relative in QWEN_IMAGE_21_REQUIRED_MODEL_FILES
            if not (self.snapshot_root / relative).is_file()
            or (self.snapshot_root / relative).stat().st_size <= 0
        ]
        if missing:
            raise ModelBootstrapPendingError(
                "Qwen-Image-2.1 snapshot is incomplete: " + ", ".join(missing)
            )
        marker = self.bootstrap_marker.read_text(encoding="utf-8").strip()
        if marker != expected:
            raise ModelBootstrapPendingError(
                "Qwen-Image-2.1 model snapshot marker does not match configured revision"
            )

    def _get_or_build_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        try:
            import torch
            from diffusers import QwenImage21Pipeline
        except ImportError as exc:
            raise RuntimeError("Qwen-Image-2.1 runtime dependencies are not installed") from exc

        if self._device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the Qwen-Image-2.1 runtime")

        started = time.monotonic()
        if self._device != "cuda":
            raise RuntimeError("Qwen-Image-2.1 worker currently requires device='cuda'")
        memory_mode = os.environ.get("QWEN_IMAGE_21_MEMORY_MODE", "int8_cuda")
        if memory_mode == "int8_cuda":
            from diffusers.quantizers import PipelineQuantizationConfig

            quantization = PipelineQuantizationConfig(
                quant_backend="bitsandbytes_8bit",
                quant_kwargs={"load_in_8bit": True},
                components_to_quantize=["transformer", "text_encoder"],
            )
            pipeline = QwenImage21Pipeline.from_pretrained(
                str(self.snapshot_root),
                dtype=torch.bfloat16,
                quantization_config=quantization,
                device_map="cuda",
                local_files_only=True,
            )
            # Fail at startup rather than silently spilling model weights to CPU.
            device_map = getattr(pipeline, "hf_device_map", None) or {}
            if any(str(device) not in ("cuda", "cuda:0", "0") for device in device_map.values()):
                raise RuntimeError(f"Qwen int8 pipeline is not fully on CUDA: {device_map}")
        elif memory_mode == "bf16_offload":
            pipeline = QwenImage21Pipeline.from_pretrained(
                str(self.snapshot_root), dtype=torch.bfloat16, local_files_only=True,
            )
            pipeline.enable_model_cpu_offload()
        else:
            raise ValueError(f"Unsupported QWEN_IMAGE_21_MEMORY_MODE: {memory_mode}")
        elapsed = time.monotonic() - started
        allocated = torch.cuda.memory_allocated() if torch.cuda.is_available() else 0
        reserved = torch.cuda.memory_reserved() if torch.cuda.is_available() else 0
        print(
            "QWEN_IMAGE_21_RUNTIME_READY "
            f"elapsed_seconds={elapsed:.3f} device={self._device} memory_mode={memory_mode} "
            f"allocated_bytes={allocated} reserved_bytes={reserved}",
            flush=True,
        )
        self._torch = torch
        self._pipeline = pipeline
        return pipeline


class QwenImage21ImageTaskRunner:
    def __init__(self, *, backend: QwenImage21Backend, task_name: str) -> None:
        if task_name not in _SUPPORTED_TASKS:
            raise ValueError(f"Unsupported Qwen-Image-2.1 task runner: {task_name}")
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
            raise ValueError(f"Qwen runner cannot execute task {request.task!r}")
        if inputs or request.inputs:
            raise ValueError("Qwen image tasks do not accept object inputs")
        if request.output.content_type != "image/png":
            raise ValueError("Qwen image output must be image/png")
        sidecars = request.sidecar_outputs or {}
        if sidecars and (
            set(sidecars) != {"metadata"}
            or sidecars["metadata"].content_type != "application/json"
        ):
            raise ValueError("Qwen benchmark requires one JSON metadata sidecar")
        parameters = QwenImage21Parameters.model_validate(request.parameters)
        output = work_dir / "image.png"
        metrics = self._backend.generate(parameters=parameters, output_path=output)
        if not output.is_file() or output.stat().st_size <= 8:
            raise RuntimeError("Qwen-Image-2.1 produced no usable PNG output")
        if not sidecars:
            return LocalArtifact(path=output, content_type="image/png")
        metadata_path = work_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(metrics, sort_keys=True) + "\n", encoding="utf-8"
        )
        return LocalArtifact(
            path=output,
            content_type="image/png",
            sidecars=(
                LocalSidecarArtifact(
                    name="metadata", path=metadata_path, content_type="application/json"
                ),
            ),
        )
