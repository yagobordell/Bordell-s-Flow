from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

type GPUQuantization = Literal["bf16", "fp8-cast", "fp8-scaled-mm"]
type GPUOffload = Literal["none", "cpu", "disk"]


class GPUDeviceProfile(BaseModel):
    """Hardware identity captured before a benchmark starts."""

    index: int = Field(ge=0)
    name: str = Field(min_length=1)
    memory_total_mib: int = Field(gt=0)


class LTXBenchmarkProfile(BaseModel):
    """Reproducible inputs that identify one LTX benchmark case."""

    label: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    ltx_source: str = Field(min_length=1)
    pipeline: str = Field(min_length=1)
    quantization: GPUQuantization
    offload: GPUOffload
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    num_frames: int = Field(gt=0)
    fps: float = Field(gt=0)
    warmup_runs: int = Field(ge=0)
    measured_runs: int = Field(ge=1)


class LTXBenchmarkSample(BaseModel):
    """One measured LTX invocation on a fixed hardware/profile combination."""

    run_index: int = Field(ge=1)
    duration_seconds: float = Field(gt=0)
    peak_gpu_memory_mib: list[int] = Field(min_length=1)
    output_size_bytes: int = Field(gt=0)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LTXBenchmarkReport(BaseModel):
    """Persisted evidence used to choose the Phase 7 GPU worker shape."""

    created_at: datetime
    profile: LTXBenchmarkProfile
    command: list[str] = Field(min_length=1)
    devices: list[GPUDeviceProfile] = Field(min_length=1)
    samples: list[LTXBenchmarkSample] = Field(min_length=1)
    mean_duration_seconds: float = Field(gt=0)
    median_duration_seconds: float = Field(gt=0)
    min_duration_seconds: float = Field(gt=0)
    max_duration_seconds: float = Field(gt=0)
    mean_end_to_end_frames_per_second: float = Field(gt=0)
    max_peak_gpu_memory_mib: list[int] = Field(min_length=1)
