from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.ports import LocalArtifact

logger = logging.getLogger(__name__)

FLUX2_KLEIN_REFERENCE_TASK = "image.flux2_klein.reference"
FLUX2_KLEIN_KEYFRAME_TASK = "image.flux2_klein.keyframe"
FLUX2_KLEIN_MODEL_ID = "black-forest-labs/FLUX.2-klein-4B"
FLUX2_KLEIN_MODEL_REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"
FLUX2_KLEIN_GENERATION_PROFILE = "flux2-klein-4b-bf16-distilled-v1"
_SUPPORTED_TASKS = frozenset({FLUX2_KLEIN_REFERENCE_TASK, FLUX2_KLEIN_KEYFRAME_TASK})


class Flux2KleinImageParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_profile: str
    model_id: str
    prompt: str = Field(min_length=1, max_length=100_000)
    width: int = Field(ge=256, le=2048)
    height: int = Field(ge=256, le=2048)
    seed: int = Field(ge=0, le=2_147_483_647)
    num_inference_steps: int = Field(default=4, ge=1, le=8)
    guidance_scale: float = Field(default=1.0, ge=0.0, le=20.0)

    @model_validator(mode="after")
    def validate_flux2_klein(self) -> Self:
        if self.generation_profile != FLUX2_KLEIN_GENERATION_PROFILE:
            raise ValueError("Unexpected FLUX.2 Klein generation profile")
        if self.model_id != FLUX2_KLEIN_MODEL_ID:
            raise ValueError(f"FLUX.2 Klein worker requires model {FLUX2_KLEIN_MODEL_ID!r}")
        if self.width % 16 or self.height % 16:
            raise ValueError("FLUX.2 Klein width and height must be divisible by 16")
        return self


def flux2_klein_application_job_id(
    *,
    task_name: str,
    prompt: str,
    width: int,
    height: int,
    model_id: str = FLUX2_KLEIN_MODEL_ID,
) -> str:
    if task_name not in _SUPPORTED_TASKS:
        raise ValueError(f"Unsupported FLUX.2 Klein application task: {task_name}")
    purpose = "reference" if task_name == FLUX2_KLEIN_REFERENCE_TASK else "keyframe"
    payload = {
        "generation_profile": FLUX2_KLEIN_GENERATION_PROFILE,
        "height": height,
        "model_id": model_id,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "purpose": purpose,
        "width": width,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"flux2-klein-{purpose}-{hashlib.sha256(canonical).hexdigest()[:32]}"


def flux2_klein_seed_for_job(job_id: str) -> int:
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


class Flux2KleinBackend:
    def __init__(
        self,
        *,
        model_root: Path,
        device: str = "cuda",
        dtype: str = "bfloat16",
    ) -> None:
        self._model_root = model_root
        self._device = device
        self._dtype = dtype
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
                raise RuntimeError("FLUX.2 Klein runtime has not been prepared")

    def generate(self, *, parameters: Flux2KleinImageParameters, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        with self._lock:
            pipeline = self._get_or_build_pipeline()
            torch = self._torch
            if torch is None:
                raise RuntimeError("FLUX.2 Klein torch runtime is unavailable")
            generator = torch.Generator(device=self._device).manual_seed(parameters.seed)
            if self._device.startswith("cuda"):
                torch.cuda.reset_peak_memory_stats()
            started = time.perf_counter()
            result = None
            try:
                with torch.inference_mode():
                    result = pipeline(
                        prompt=parameters.prompt,
                        width=parameters.width,
                        height=parameters.height,
                        guidance_scale=parameters.guidance_scale,
                        num_inference_steps=parameters.num_inference_steps,
                        generator=generator,
                    )
                images = result.images
                if len(images) != 1:
                    raise RuntimeError("FLUX.2 Klein did not return exactly one image")
                image = images[0]
                if image.size != (parameters.width, parameters.height):
                    raise RuntimeError("FLUX.2 Klein returned unexpected image dimensions")
                image.save(output_path, format="PNG")
                _validate_png(output_path, width=parameters.width, height=parameters.height)
            finally:
                elapsed = time.perf_counter() - started
                peak_vram = 0
                if self._device.startswith("cuda"):
                    peak_vram = int(torch.cuda.max_memory_allocated())
                    torch.cuda.empty_cache()
                logger.info(
                    "FLUX2_KLEIN_INFERENCE_METRICS seconds=%.3f peak_vram_bytes=%d "
                    "width=%d height=%d steps=%d guidance=%.3f seed=%d",
                    elapsed,
                    peak_vram,
                    parameters.width,
                    parameters.height,
                    parameters.num_inference_steps,
                    parameters.guidance_scale,
                    parameters.seed,
                )
                result = None

    def _validate_bootstrap(self) -> None:
        expected = f"{FLUX2_KLEIN_MODEL_ID}@{FLUX2_KLEIN_MODEL_REVISION}"
        if (
            not self.bootstrap_marker.is_file()
            or self.bootstrap_marker.read_text(encoding="utf-8").strip() != expected
            or not self.snapshot_root.is_dir()
        ):
            raise ModelBootstrapPendingError("FLUX.2 Klein model snapshot is not ready")

    def _get_or_build_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        try:
            import torch
            from diffusers import Flux2KleinPipeline
        except ImportError as exc:
            raise RuntimeError("FLUX.2 Klein runtime dependencies are not installed") from exc

        if self._device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available for the FLUX.2 Klein runtime")
        if self._dtype != "bfloat16":
            raise RuntimeError("FLUX.2 Klein production worker currently requires bfloat16")

        pipeline = Flux2KleinPipeline.from_pretrained(
            str(self.snapshot_root),
            torch_dtype=torch.bfloat16,
            local_files_only=True,
            low_cpu_mem_usage=True,
        )
        pipeline.to(self._device)
        pipeline.set_progress_bar_config(disable=True)
        self._torch = torch
        self._pipeline = pipeline
        return pipeline


def _validate_png(path: Path, *, width: int, height: int) -> None:
    if not path.is_file() or path.stat().st_size <= 8:
        raise RuntimeError("FLUX.2 Klein produced no usable PNG output")
    with Image.open(path) as image:
        if image.format != "PNG":
            raise RuntimeError("FLUX.2 Klein output is not a PNG")
        if image.size != (width, height):
            raise RuntimeError("FLUX.2 Klein PNG has unexpected dimensions")
        image.verify()


class Flux2KleinImageTaskRunner:
    def __init__(self, *, backend: Flux2KleinBackend, task_name: str) -> None:
        if task_name not in _SUPPORTED_TASKS:
            raise ValueError(f"Unsupported FLUX.2 Klein task runner: {task_name}")
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
            raise ValueError(f"FLUX.2 Klein runner cannot execute task {request.task!r}")
        if inputs or request.inputs:
            raise ValueError("FLUX.2 Klein fallback tasks do not accept object inputs")
        if request.output.content_type != "image/png":
            raise ValueError("FLUX.2 Klein output must be image/png")
        parameters = Flux2KleinImageParameters.model_validate(request.parameters)
        output = work_dir / "image.png"
        self._backend.generate(parameters=parameters, output_path=output)
        _validate_png(output, width=parameters.width, height=parameters.height)
        return LocalArtifact(path=output, content_type="image/png")
