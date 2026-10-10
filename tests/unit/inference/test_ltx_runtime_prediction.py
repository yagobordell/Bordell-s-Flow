from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ai_video_factory.inference.ltx_runtime_prediction import (
    degraded_instance_ids,
    forecast_ltx_job_seconds,
    typical_forecast_seconds,
)
from ai_video_factory.inference.salad_autoscaler import (
    PredictiveAutoscalerConfig,
    build_global_demands,
)


NOW = datetime(2026, 10, 10, tzinfo=UTC)


def job(*, frames: int = 153, profile: str = "fp8") -> dict[str, object]:
    return {
        "generation_profile": profile, "width": "1920", "height": "1080",
        "num_frames": str(frames), "status": "pending", "run_id": "video02",
    }


def sample(instance: str, i: int, spf: float, frames: int = 153) -> dict[str, object]:
    return {
        "job_id": f"{instance}-{i}-{frames}", "instance_id": instance,
        "instance_identity_source": "salad_instance", "attempt_count": 1,
        "pipeline_reused": True, "stage": "ltx25_i2v",
        "profile": "fp8", "width": 1920, "height": 1088,
        "num_frames": frames, "task_seconds": spf * frames,
        "inference_seconds": spf * frames, "recorded_at": NOW,
    }


def test_exact_warm_geometry_prediction_excludes_degraded_instance() -> None:
    history = [
        *[sample("fast", i, 1.7) for i in range(10)],
        *[sample("slow", i, 14.4) for i in range(7)],
    ]
    excluded = degraded_instance_ids(history, now=NOW)
    assert excluded == frozenset({"slow"})
    assert forecast_ltx_job_seconds(
        job(), history, stage="ltx25_i2v", fallback=305,
        excluded_instances=excluded,
    ) == pytest.approx(260.1)


def test_profiles_and_geometry_are_not_cross_contaminated() -> None:
    history = [sample("fast", i, 1.7) for i in range(10)]
    assert forecast_ltx_job_seconds(
        job(profile="different"), history, stage="ltx25_i2v", fallback=305,
    ) == 305
    assert forecast_ltx_job_seconds(
        {**job(), "height": "720"}, history, stage="ltx25_i2v", fallback=305,
    ) == 305


def test_sparse_history_falls_back_and_nearby_frames_can_scale() -> None:
    history = [sample("a", i, 2, frames=97) for i in range(6)]
    assert forecast_ltx_job_seconds(
        job(frames=153), history, stage="ltx25_i2v", fallback=305,
    ) == 306
    assert forecast_ltx_job_seconds(
        job(frames=409), history, stage="ltx25_i2v", fallback=305,
    ) == 305


def test_forecast_rollup_is_only_for_display_not_queue_sum() -> None:
    assert typical_forecast_seconds(
        [{"forecast_runtime_seconds": 100}, {"forecast_runtime_seconds": 300}], 50
    ) == 200


def test_project_demand_sums_individual_clip_forecasts() -> None:
    cfg = PredictiveAutoscalerConfig(
        enabled=True, project_max_replicas=3, target_drain_seconds=3600,
        cold_start_seconds=180, target_utilization=0.85, runtime_percentile=0.75,
        runtime_history_limit=200, downscale_stable_polls=2,
        max_upscale_per_poll=5, max_downscale_per_poll=5,
        active_deletion_cost=100000, idle_deletion_cost=0,
        stage_max_replicas={"ltx25_i2v": 3},
        fallback_runtime_seconds={"ltx25_i2v": 300},
    )
    rows = [
        {**job(), "forecast_runtime_seconds": 260},
        {**job(frames=201), "forecast_runtime_seconds": 520},
        {**job(frames=97), "forecast_runtime_seconds": 155},
    ]
    result = build_global_demands(
        rows_by_stage={"ltx25_i2v": rows},
        runtime_by_stage={"ltx25_i2v": 300}, config=cfg, now=NOW,
    )["ltx25_i2v"]
    assert result.remaining_work_seconds == 935
    assert result.ideal_replicas == 1
