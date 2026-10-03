"""Explicitly opt-in paired I2V task for the unmerged Salad experiment.

Both videos belong to ONE atomic Postgres job and are published via the
existing durable primary+sidecar bundle. No implicit coalescing of unrelated
production jobs, output replay or external-client ID reassignment.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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

    @model_validator(mode="after")
    def check_pair(self) -> Batch2Parameters:
        a, b = self.a, self.b
        if (a.generation_profile, b.generation_profile) != (
            LTX_PRODUCTION_I2V_PROFILE, LTX_PRODUCTION_I2V_PROFILE,
        ):
            raise ValueError("Batch2 must use the exact validated production FP8 I2V recipe")
        if (a.width, a.height, a.fps, a.num_frames) != (
            1280, 720, 24, 121,
        ) or (b.width, b.height, b.fps, b.num_frames) != (
            1280, 720, 24, 121,
        ):
            raise ValueError("Experimental batch2 is restricted to two 5s 720p/24fps videos")
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
