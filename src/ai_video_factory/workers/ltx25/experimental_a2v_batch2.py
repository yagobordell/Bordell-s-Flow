"""Opt-in atomic 1080p A2V GPU-B2 task. Never selected by production A2V."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.gpu_failures import is_retryable_gpu_failure
from ai_video_factory.inference.ports import LocalArtifact, LocalSidecarArtifact

from .a2v import (
    DirectLTX25AudioToVideoBackend,
    LTX_A2V_RUN_STATIC_FAST_PROFILE,
    LTXAudioToVideoParameters,
)

LTX_A2V_BATCH2_TASK = "video.ltx25.audio_to_video_batch2_experimental"


class A2VBatch2Parameters(BaseModel):
    """Exactly two static-reference inputs with independent audio and RNG."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    a: LTXAudioToVideoParameters
    b: LTXAudioToVideoParameters
    stage2_mode: Literal["cpu"] = "cpu"
    accept_full_hd_oom_risk: bool = False

    @model_validator(mode="after")
    def check_pair(self) -> A2VBatch2Parameters:
        if any(
            p.generation_profile != LTX_A2V_RUN_STATIC_FAST_PROFILE
            for p in (self.a, self.b)
        ):
            raise ValueError("A2V B2 must preserve the approved static reference recipe")
        if any((p.width, p.height, p.fps) != (1920, 1080, 24)
               for p in (self.a, self.b)):
            raise ValueError("A2V B2 requires matching 1920x1080 at 24fps")
        if self.a.seed == self.b.seed:
            raise ValueError("A2V B2 requires distinct independent seeds")
        if not self.accept_full_hd_oom_risk:
            raise ValueError("Explicitly accept experimental A2V Full HD B2 GPU OOM risk")
        return self


class ExperimentalA2VBatch2TaskRunner:
    task_name = LTX_A2V_BATCH2_TASK

    def __init__(self, backend: DirectLTX25AudioToVideoBackend) -> None:
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
        required = {"image_a", "audio_a", "image_b", "audio_b"}
        if request.task != self.task_name or set(inputs) != required:
            raise ValueError("A2V B2 requires image_a/audio_a/image_b/audio_b")
        if {entry.name for entry in request.inputs} != required:
            raise ValueError("A2V B2 declared inputs must match downloaded inputs")
        if request.output.content_type != "video/mp4" or not request.output.key.endswith(".mp4"):
            raise ValueError("A2V B2 primary output must be MP4")
        sidecars = request.sidecar_outputs or {}
        if set(sidecars) != {"video_b", "report"}:
            raise ValueError("A2V B2 requires video_b and report sidecars")
        if (
            sidecars["video_b"].content_type != "video/mp4"
            or sidecars["report"].content_type != "application/json"
            or not sidecars["video_b"].key.endswith(".mp4")
            or not sidecars["report"].key.endswith(".json")
        ):
            raise ValueError("Invalid A2V B2 atomic sidecar contract")
        params = A2VBatch2Parameters.model_validate(request.parameters)
        first, second = work_dir / "batch2-a.mp4", work_dir / "batch2-b.mp4"
        try:
            report: dict[str, Any] = self._backend.generate_pair(
                image_a=inputs["image_a"], audio_a=inputs["audio_a"],
                image_b=inputs["image_b"], audio_b=inputs["audio_b"],
                output_a=first, output_b=second,
                parameters_a=params.a, parameters_b=params.b,
            )
        except Exception as exc:
            if is_retryable_gpu_failure(exc):
                self._backend.invalidate_pipeline()
            raise
        if report.get("actual_gpu_batch_size") != 2 or report.get("mode") != "batch2":
            raise RuntimeError("A2V B2 cannot return sequential fallback as real GPU batch2")
        for path in (first, second):
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("A2V B2 output missing: " + path.name)
        meta = work_dir / "batch2-report.json"
        meta.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return LocalArtifact(
            path=first, content_type="video/mp4",
            sidecars=(
                LocalSidecarArtifact(
                    name="video_b", path=second, content_type="video/mp4",
                ),
                LocalSidecarArtifact(
                    name="report", path=meta, content_type="application/json",
                ),
            ),
        )
