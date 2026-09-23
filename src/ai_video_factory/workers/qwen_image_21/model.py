from __future__ import annotations

import gc
import hashlib
import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact

QWEN_IMAGE_21_REFERENCE_TASK = "image.qwen_image_21.reference"
QWEN_IMAGE_21_KEYFRAME_TASK = "image.qwen_image_21.keyframe"
QWEN_IMAGE_21_MODEL_ID = "Qwen/Qwen-Image-2.1"
QWEN_IMAGE_21_MODEL_REVISION = "b3179ad355be050328e483a9dfdd9e60cd62adfa"
QWEN_IMAGE_21_GENERATION_PROFILE = "qwen-image-2.1-bf16-40step-v1"
_SUPPORTED_TASKS = frozenset({QWEN_IMAGE_21_REFERENCE_TASK, QWEN_IMAGE_21_KEYFRAME_TASK})


class QwenImage21Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    model_id: str
    model_revision: str
    prompt: str = Field(min_length=1, max_length=100_000)
    width: int = Field(ge=256, le=2048)
    height: int = Field(ge=256, le=2048)
    seed: int = Field(ge=0, le=2_147_483_647)
    num_inference_steps: int = Field(default=40, ge=1, le=100)

    @model_validator(mode="after")
    def validate_qwen_image_21(self) -> Self:
        if self.generation_profile != QWEN_IMAGE_21_GENERATION_PROFILE:
            raise ValueError("Unexpected Qwen-Image-2.1 generation profile")
        if self.model_id != QWEN_IMAGE_21_MODEL_ID:
            raise ValueError(f"Qwen worker requires model {QWEN_IMAGE_21_MODEL_ID!r}")
        if self.model_revision != QWEN_IMAGE_21_MODEL_REVISION:
            raise ValueError(
                f"Qwen worker requires revision {QWEN_IMAGE_21_MODEL_REVISION!r}"
            )
        if self.width % 16 or self.height % 16:
            raise ValueError("Qwen-Image-2.1 width and height must be divisible by 16")
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

    def generate(self, *, parameters: QwenImage21Parameters, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        with self._lock:
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
                    generator=generator,
                )
                images = result.images
                if len(images) != 1:
                    raise RuntimeError("Qwen-Image-2.1 did not return exactly one image")
                image = images[0]
                if image.size != (parameters.width, parameters.height):
                    raise RuntimeError("Qwen-Image-2.1 returned unexpected image dimensions")
                image.save(output_path, format="PNG")
                elapsed = time.monotonic() - started
                peak_allocated = torch.cuda.max_memory_allocated() if is_cuda else 0
                peak_reserved = torch.cuda.max_memory_reserved() if is_cuda else 0
                print(
                    "QWEN_IMAGE_21_INFERENCE_METRIC "
                    f"elapsed_seconds={elapsed:.3f} seed={parameters.seed} "
                    f"width={parameters.width} height={parameters.height} "
                    f"steps={parameters.num_inference_steps} "
                    f"peak_allocated_bytes={peak_allocated} peak_reserved_bytes={peak_reserved}",
                    flush=True,
                )
            finally:
                del result
                gc.collect()
                if is_cuda:
                    torch.cuda.empty_cache()

    def _validate_bootstrap(self) -> None:
        expected = f"{QWEN_IMAGE_21_MODEL_ID}@{QWEN_IMAGE_21_MODEL_REVISION}"
        if not self.bootstrap_marker.is_file() or not self.snapshot_root.is_dir():
            raise ModelBootstrapPendingError("Qwen-Image-2.1 model snapshot is not ready")
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
        pipeline = QwenImage21Pipeline.from_pretrained(
            str(self.snapshot_root),
            torch_dtype=torch.bfloat16,
            local_files_only=True,
        )
        if self._device != "cuda":
            raise RuntimeError("Qwen-Image-2.1 worker currently requires device='cuda'")
        pipeline.enable_model_cpu_offload()
        elapsed = time.monotonic() - started
        allocated = torch.cuda.memory_allocated() if torch.cuda.is_available() else 0
        reserved = torch.cuda.memory_reserved() if torch.cuda.is_available() else 0
        print(
            "QWEN_IMAGE_21_RUNTIME_READY "
            f"elapsed_seconds={elapsed:.3f} device={self._device} "
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
        parameters = QwenImage21Parameters.model_validate(request.parameters)
        output = work_dir / "image.png"
        self._backend.generate(parameters=parameters, output_path=output)
        if not output.is_file() or output.stat().st_size <= 8:
            raise RuntimeError("Qwen-Image-2.1 produced no usable PNG output")
        return LocalArtifact(path=output, content_type="image/png")
