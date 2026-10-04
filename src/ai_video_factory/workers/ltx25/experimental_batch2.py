"""Explicitly opt-in paired I2V task for the unmerged Salad experiment.

Both videos belong to ONE atomic Postgres job and are published via the
existing durable primary+sidecar bundle. No implicit coalescing of unrelated
production jobs, output replay or external-client ID reassignment.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.gpu_failures import is_retryable_gpu_failure
from ai_video_factory.inference.ports import LocalArtifact, LocalSidecarArtifact

from .model import LTX_PRODUCTION_I2V_PROFILE, DirectLTX25Backend, LTXVideoParameters

LTX_I2V_BATCH2_TASK = "video.ltx25.experimental_batch2"


class Batch2Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    a: LTXVideoParameters
    b: LTXVideoParameters
    allow_sequential_fallback: bool = False
    reuse_transformer_across_stages: bool = False
    accept_full_hd_batch2_oom_risk: bool = False
    full_hd_stage2_mode: str = "cpu"
    vae_backend: Literal["eager", "triton_experimental"] = "eager"

    @model_validator(mode="after")
    def check_pair(self) -> Batch2Parameters:
        a, b = self.a, self.b
        if self.full_hd_stage2_mode not in ("cpu", "disk"):
            raise ValueError("Full HD stage2 mode must be cpu or disk")
        if (a.generation_profile, b.generation_profile) != (
            LTX_PRODUCTION_I2V_PROFILE, LTX_PRODUCTION_I2V_PROFILE,
        ):
            raise ValueError("Batch2 must use the exact validated production FP8 I2V recipe")
        geometry = (a.width, a.height, a.fps, a.num_frames)
        if geometry != (b.width, b.height, b.fps, b.num_frames):
            raise ValueError("Batch2 requires matching video geometries and frame counts")
        if self.vae_backend == "triton_experimental" and (
            geometry != (1920, 1080, 24, 121) or self.full_hd_stage2_mode != "cpu"
        ):
            raise ValueError("Experimental Triton VAE requires 1080p batch2 with CPU stage2")
        if geometry == (1920, 1080, 24, 121):
            if not self.accept_full_hd_batch2_oom_risk:
                raise ValueError(
                    "1080p batch2 is unvalidated on a 32-GiB RTX 5090: "
                    "explicitly accept the Full HD OOM risk before this experiment"
                )
            if self.allow_sequential_fallback or self.reuse_transformer_across_stages:
                raise ValueError(
                    "Unvalidated 1080p batch2 forbids sequential fallback and "
                    "unvalidated shared-stage transformer residency"
                )
        elif geometry != (1280, 720, 24, 121):
            raise ValueError(
                "Experimental batch2 only supports matching 5s 720p clips, "
                "or explicit-risk matching 5s 1080p clips"
            )
        if a.seed == b.seed:
            raise ValueError("Samples must have distinct RNG seeds")
        return self


class ExperimentalBatch2TaskRunner:
    task_name = LTX_I2V_BATCH2_TASK

    def __init__(self, backend: DirectLTX25Backend) -> None:
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
            raise ValueError("Wrong batch2 task")
        if set(inputs) != {"keyframe_a", "keyframe_b"}:
            raise ValueError("Batch2 requires exactly keyframe_a and keyframe_b")
        if {obj.name for obj in request.inputs} != set(inputs):
            raise ValueError("Batch2 declared input names do not match downloaded inputs")
        if request.output.content_type != "video/mp4" or not request.output.key.endswith(".mp4"):
            raise ValueError("Batch2 primary output must be an MP4")
        sidecars = request.sidecar_outputs or {}
        if set(sidecars) != {"video_b", "report"}:
            raise ValueError("Batch2 requires video_b and report sidecars")
        if (sidecars["video_b"].content_type, sidecars["report"].content_type) != (
            "video/mp4", "application/json",
        ):
            raise ValueError("Batch2 sidecar content types are invalid")
        if (
            not sidecars["video_b"].key.endswith(".mp4")
            or not sidecars["report"].key.endswith(".json")
        ):
            raise ValueError("Batch2 sidecar key extensions are invalid")
        params = Batch2Parameters.model_validate(request.parameters)
        output_a = work_dir / "batch2-a.mp4"
        output_b = work_dir / "batch2-b.mp4"
        report_path = work_dir / "batch2-report.json"
        try:
            report: dict[str, Any] = self._backend.generate_pair(
                keyframe_a=inputs["keyframe_a"],
                keyframe_b=inputs["keyframe_b"],
                output_a=output_a, output_b=output_b,
                parameters_a=params.a, parameters_b=params.b,
                allow_sequential_fallback=params.allow_sequential_fallback,
                reuse_transformer_across_stages=params.reuse_transformer_across_stages,
                accept_full_hd_batch2_oom_risk=params.accept_full_hd_batch2_oom_risk,
                full_hd_stage2_mode=params.full_hd_stage2_mode,
                **({"vae_backend": params.vae_backend}
                   if params.vae_backend != "eager" else {}),
            )
        except Exception as exc:
            if is_retryable_gpu_failure(exc):
                self._backend.invalidate_pipeline()
            raise
        for path in (output_a, output_b):
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Batch2 generated an empty or missing video: {path.name}")
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return LocalArtifact(
            path=output_a,
            content_type="video/mp4",
            sidecars=(
                LocalSidecarArtifact(name="video_b", path=output_b, content_type="video/mp4"),
                LocalSidecarArtifact(
                    name="report", path=report_path, content_type="application/json"
                ),
            ),
        )
