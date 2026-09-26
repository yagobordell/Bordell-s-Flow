from __future__ import annotations

from copy import deepcopy
from typing import Any

from ai_video_factory.workers.ltx25.comparison import (
    METADATA_RECIPE_FIELDS,
    TIMING_FIELDS,
    compare_runs,
)


def _approved_pair() -> dict[str, Any]:
    request = {
        "job_id": "monje-original",
        "inputs": [
            {"name": "image", "sha256": "a" * 64},
            {"name": "audio", "sha256": "b" * 64},
        ],
        "parameters": {
            "prompt": "An elderly monk speaking calmly to the camera.",
            "seed": 4242,
            "width": 1280,
            "height": 720,
            "fps": 24,
            "generation_profile": "reference",
        },
    }
    baseline_metadata: dict[str, Any] = {
        field: "same-pinned-value" for field in METADATA_RECIPE_FIELDS
    }
    baseline_metadata.update(
        {
            "ltx_model_revision": "6c7e5e573ac1667efc83407806fe9b0b93730e60",
            "pipeline_reused": False,
            "inference_seconds": 100.0,
            "model_load_seconds": 50.0,
            "video_encode_mux_seconds": 3.0,
            "generation_elapsed_seconds": 153.0,
            "total_elapsed_seconds": 160.0,
        }
    )
    candidate_request = deepcopy(request)
    candidate_request["job_id"] = "monje-independent"
    candidate_metadata = deepcopy(baseline_metadata)
    candidate_metadata["inference_seconds"] = 90.0
    candidate_metadata["generation_elapsed_seconds"] = 143.0
    candidate_metadata["total_elapsed_seconds"] = 150.0
    return {
        "baseline_request": request,
        "baseline_metadata": baseline_metadata,
        "candidate_request": candidate_request,
        "candidate_metadata": candidate_metadata,
        "baseline_commit": "1" * 40,
        "candidate_commit": "2" * 40,
        "baseline_docker_digest": "sha256:" + "c" * 64,
        "candidate_docker_digest": "sha256:" + "d" * 64,
        "baseline_visual_approved": True,
        "candidate_visual_approved": True,
        "expected_image_sha256": "a" * 64,
        "expected_audio_sha256": "b" * 64,
    }


def test_offline_comparison_requires_matching_inputs_recipe_and_human_approval() -> None:
    report = compare_runs(**_approved_pair())
    assert report["can_compare_generation_times"] is True
    assert report["pipeline_temperature"] == "cold"
    assert report["quality_inferred_from_technical_checks"] is False
    assert report["blocking_reasons"] == []
    assert report["candidate_minus_baseline_inference_seconds"] == -10.0
    assert set(report["worker_timings"]["baseline"]) == set(TIMING_FIELDS)


def test_offline_comparison_never_infers_lipsync_approval_from_metadata() -> None:
    runs = _approved_pair()
    runs["baseline_visual_approved"] = False
    runs["candidate_visual_approved"] = False
    report = compare_runs(**runs)
    assert report["can_compare_generation_times"] is False
    assert report["candidate_minus_baseline_inference_seconds"] is None
    assert len([reason for reason in report["blocking_reasons"] if "visual" in reason]) == 2


def test_offline_comparison_blocks_changed_input_hash() -> None:
    runs = _approved_pair()
    runs["candidate_request"]["inputs"][1]["sha256"] = "f" * 64
    report = compare_runs(**runs)
    assert not report["can_compare_generation_times"]
    assert any("input SHA-256" in reason for reason in report["blocking_reasons"])


def test_offline_comparison_blocks_changed_generation_recipe() -> None:
    runs = _approved_pair()
    runs["candidate_metadata"]["stage_1_image_strength"] = 1.0
    report = compare_runs(**runs)
    assert not report["can_compare_generation_times"]
    assert any("stage_1_image_strength" in reason for reason in report["blocking_reasons"])


def test_offline_comparison_keeps_cold_and_warm_runs_separate() -> None:
    runs = _approved_pair()
    runs["candidate_metadata"]["pipeline_reused"] = True
    report = compare_runs(**runs)
    assert not report["can_compare_generation_times"]
    assert report["pipeline_temperature"] == "not_comparable"


def test_offline_comparison_blocks_replayed_job_id() -> None:
    runs = _approved_pair()
    runs["candidate_request"]["job_id"] = runs["baseline_request"]["job_id"]
    report = compare_runs(**runs)
    assert not report["can_compare_generation_times"]
    assert any("job_id" in reason for reason in report["blocking_reasons"])


def test_offline_comparison_requires_immutable_image_provenance() -> None:
    runs = _approved_pair()
    runs["candidate_docker_digest"] = None
    report = compare_runs(**runs)
    assert not report["can_compare_generation_times"]
    assert any("Docker" in reason for reason in report["blocking_reasons"])


def test_offline_comparison_rejects_missing_worker_timing() -> None:
    runs = _approved_pair()
    del runs["candidate_metadata"]["inference_seconds"]
    report = compare_runs(**runs)
    assert not report["can_compare_generation_times"]
    assert report["candidate_minus_baseline_inference_seconds"] is None
