from __future__ import annotations

import pytest

from ai_video_factory.inference.ltx_instance_scoring import classify_ltx_instances


def _job(instance, ordinal, seconds, *, profile="p1", frames=97):
    return {
        "job_id": f"{instance}-{ordinal}-{profile}",
        "instance_id": instance,
        "stage": "ltx25",
        "profile": profile,
        "width": 1920,
        "height": 1080,
        "num_frames": frames,
        "inference_seconds": seconds,
    }


def test_persistent_slow_machine_is_only_flagged_against_comparable_peers():
    rows = [
        *[_job("fast1", i, 97.0) for i in range(3)],
        *[_job("fast2", i, 100.0) for i in range(3)],
        *[_job("slow", i, 320.0) for i in range(3)],
    ]
    scored = classify_ltx_instances(rows)
    assert next(x for x in scored if x["instance_id"] == "slow")[
        "classification"
    ] == "suspected_slow"
    assert all(
        x["classification"] == "normal"
        for x in scored if x["instance_id"] != "slow"
    )


def test_one_slow_job_cannot_trigger_quarantine_signal():
    jobs = [
        *[_job("fast1", i, 97.0) for i in range(3)],
        *[_job("fast2", i, 98.0) for i in range(3)],
        _job("slow", 1, 380.0),
    ]
    slow = next(
        item for item in classify_ltx_instances(jobs)
        if item["instance_id"] == "slow"
    )
    assert slow["classification"] == "insufficient_comparable_data"
    assert slow["confidence"] == "insufficient"


def test_profiles_and_lengths_are_never_compared_across_cohorts():
    jobs = [
        *[_job("a", i, 97.0) for i in range(3)],
        *[_job("b", i, 400.0, profile="other") for i in range(3)],
        *[_job("c", i, 420.0, frames=161) for i in range(3)],
    ]
    assert all(
        x["classification"] == "insufficient_comparable_data"
        for x in classify_ltx_instances(jobs)
    )


def test_duplicate_job_metric_is_rejected():
    record = _job("a", 1, 97.0)
    with pytest.raises(ValueError, match="Duplicate"):
        classify_ltx_instances([record, record])
