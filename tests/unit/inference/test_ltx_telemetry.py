from __future__ import annotations

from types import SimpleNamespace

from ai_video_factory.inference.ltx_telemetry import build_ltx_job_telemetry
from ai_video_factory.inference.ltx_telemetry_analysis import compare_ltx_telemetry


def _request(task: str = "video.ltx25.generate") -> SimpleNamespace:
    return SimpleNamespace(
        job_id="job-1",
        task=task,
        parameters={
            "generation_profile": "same-recipe", "width": 1920,
            "height": 1080, "num_frames": 121,
        },
        fingerprint=lambda: "a" * 64,
    )


def test_i2v_telemetry_captures_only_approved_timings() -> None:
    row = build_ltx_job_telemetry(
        _request(),
        {
            "pipeline_call_seconds": 160.5,
            "stage1_total_seconds": 95.0,
            "stage2_total_seconds": 54.0,
            "video_decode_seconds": 4.0,
            "pipeline_reused": True,
            "internal_width": 1920,
            "internal_height": 1088,
            "prompt": "must never be persisted",
        },
        worker_id="worker-1", instance_id="instance-1",
        attempt_count=1, task_seconds=178.1,
    )
    assert row is not None
    assert row["stage"] == "ltx25_i2v"
    assert row["instance_identity_source"] == "salad_instance"
    assert row["height"] == 1088
    assert row["inference_seconds"] == 160.5
    assert row["stage1_seconds"] == 95.0
    assert row["video_decode_seconds"] == 4.0
    assert "prompt" not in row
    assert "prompt" not in row["phase_seconds"]


def test_a2v_telemetry_uses_pipeline_inference_and_rejects_invalid_timing() -> None:
    row = build_ltx_job_telemetry(
        _request("video.ltx25.audio_to_video"),
        {"pipeline_inference_seconds": 50.0, "model_load_seconds": 5.0,
         "num_frames": 137, "stage1_diffusion_seconds": 31.0,
         "stage2_diffusion_seconds": float("nan")},
        worker_id="w", instance_id="i", attempt_count=2, task_seconds=63.0,
    )
    assert row is not None
    assert row["stage"] == "ltx25"
    assert row["num_frames"] == 137
    assert row["stage1_seconds"] == 31.0
    assert row["stage2_seconds"] is None
    assert row["pipeline_load_seconds"] == 5.0


def test_telemetry_skips_other_jobs_and_missing_metrics() -> None:
    assert build_ltx_job_telemetry(
        _request("image.upscale"), {}, worker_id="w", instance_id=None,
        attempt_count=1, task_seconds=1.0,
    ) is None
    assert build_ltx_job_telemetry(
        _request(), None, worker_id="w", instance_id=None,
        attempt_count=1, task_seconds=1.0,
    ) is None


def _record(job: str, instance: str, frames: int, spf: float) -> dict[str, object]:
    return {
        "job_id": job, "attempt_count": 1, "instance_id": instance,
        "stage": "ltx25", "profile": "p", "width": 1920,
        "height": 1088, "num_frames": frames,
        "inference_seconds": frames * spf,
    }


def test_comparison_requires_exact_matching_frames() -> None:
    rows = [
        _record(f"slow-{i}", "slow", 139, 3.0) for i in range(3)
    ] + [
        _record(f"fast-{i}", "fast", 147, 0.9) for i in range(3)
    ]
    result = compare_ltx_telemetry(rows)
    assert all(x["classification"] == "insufficient_exact_frame_matches" for x in result)


def test_comparison_flags_slow_only_in_shared_frame_cohort() -> None:
    rows = [
        _record(f"slow-{i}", "slow", 139, 3.0) for i in range(3)
    ] + [
        _record(f"fast-{i}", "fast", 139, 0.9) for i in range(3)
    ]
    result = compare_ltx_telemetry(rows)
    assert next(x for x in result if x["instance_id"] == "slow")[
        "classification"
    ] == "review_candidate"
    assert next(x for x in result if x["instance_id"] == "fast")[
        "classification"
    ] == "comparable"
    assert next(x for x in result if x["instance_id"] == "slow")[
        "median_matched_ratio"
    ] == 3.333


def test_comparison_keeps_profiles_and_dimensions_separate() -> None:
    rows = [
        _record(f"a-{i}", "slow", 139, 3.0) for i in range(3)
    ] + [
        {**_record(f"b-{i}", "fast", 139, 0.8), "profile": "different"}
        for i in range(3)
    ]
    result = compare_ltx_telemetry(rows)
    assert len(result) == 2
    assert all(x["classification"] == "insufficient_exact_frame_matches" for x in result)


def test_missing_provider_instance_uses_worker_process_surrogate() -> None:
    row = build_ltx_job_telemetry(
        _request(),
        {"pipeline_call_seconds": 7.5},
        worker_id="host-1234",
        instance_id=None,
        attempt_count=1,
        task_seconds=8.0,
    )
    assert row is not None
    assert row["instance_id"] == "host-1234"
    assert row["instance_identity_source"] == "worker_process"
