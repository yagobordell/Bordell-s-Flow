from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_video_factory.inference.ltx_performance_health import assess_ltx_instance_health

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def job(instance: str, number: int, spf: float, *, frames: int = 153,
        profile: str = "fp8", source: str = "salad_instance",
        reused: bool = True, stage: str = "ltx25_i2v") -> dict[str, object]:
    return {
        "job_id": f"{instance}-{number}-{stage}-{profile}-{frames}",
        "instance_id": instance, "instance_identity_source": source,
        "stage": stage, "profile": profile, "width": 1920, "height": 1088,
        "num_frames": frames, "inference_seconds": spf * frames,
        "attempt_count": 1, "pipeline_reused": reused,
        "recorded_at": NOW - timedelta(hours=1),
    }


def state(results: list[dict[str, object]], instance: str) -> str:
    return next(row for row in results if row["instance_id"] == instance)["state"]


def test_video02_pattern_detected_without_penalizing_fast_peer() -> None:
    rows = [
        *[job("fast", i, 1.7, frames=153 if i % 2 else 97) for i in range(30)],
        *[job("slow", i, 14.4, frames=153 if i % 2 else 97) for i in range(11)],
    ]
    assessed = assess_ltx_instance_health(rows, now=NOW)
    assert state(assessed, "slow") == "confirmed_degraded"
    assert state(assessed, "fast") == "healthy"
    slow = next(row for row in assessed if row["instance_id"] == "slow")
    assert 8.0 < slow["ratio_to_exact_peers"] < 9.0
    assert slow["exact_matched_jobs"] == 11


def test_single_instance_or_no_exact_matches_cannot_be_confirmed() -> None:
    assert state(assess_ltx_instance_health(
        [job("one", i, 9) for i in range(9)], now=NOW,
    ), "one") == "insufficient_data"
    rows = [job("a", i, 1.0, frames=97) for i in range(6)] + [
        job("b", i, 9.0, frames=153) for i in range(6)
    ]
    assert all(row["state"] == "insufficient_data"
               for row in assess_ltx_instance_health(rows, now=NOW))


def test_normal_difference_or_insufficient_samples_is_not_a_degradation() -> None:
    rows = [job("a", i, 1.0) for i in range(6)] + [
        job("b", i, 1.12) for i in range(6)
    ] + [job("c", i, 8.0) for i in range(2)]
    assessed = assess_ltx_instance_health(rows, now=NOW)
    assert state(assessed, "a") == "healthy"
    assert state(assessed, "b") == "healthy"
    assert state(assessed, "c") == "insufficient_data"


def test_non_comparable_and_untrusted_observations_are_excluded() -> None:
    fast = [job("fast", i, 1.0) for i in range(9)]
    slow = [
        job("slow", i, 9.0, profile="other" if i % 3 == 0 else "fp8",
            reused=i % 3 != 1, source="worker_process" if i % 3 == 2
            else "salad_instance") for i in range(9)
    ]
    assessed = assess_ltx_instance_health(fast + slow, now=NOW)
    assert state(assessed, "slow") == "insufficient_data"


def test_invalid_duplicate_stale_and_future_rows_do_not_create_false_positive() -> None:
    fast = [job("fast", i, 1) for i in range(6)]
    old = {**job("slow", 7, 10), "recorded_at": NOW - timedelta(days=8)}
    future = {**job("slow", 8, 10), "recorded_at": NOW + timedelta(days=1)}
    duplicate = job("slow", 9, 10)
    incomplete = {**job("slow", 10, 10), "inference_seconds": None}
    assessed = assess_ltx_instance_health(
        fast + [old, future, duplicate, duplicate, incomplete], now=NOW,
    )
    assert state(assessed, "fast") == "insufficient_data"
    assert not any(row["instance_id"] == "slow" for row in assessed)


def test_cold_start_and_provisional_retry_do_not_count_as_completed_warm() -> None:
    rows = [job("fast", i, 1) for i in range(6)]
    rows += [{**job("slow", i, 8), "pipeline_reused": False} for i in range(4)]
    rows += [{**job("slow", i + 10, 8), "attempt_count": 2} for i in range(5)]
    assessed = assess_ltx_instance_health(rows, now=NOW)
    assert state(assessed, "fast") == "insufficient_data"


def test_threshold_validation_is_fail_closed() -> None:
    with pytest.raises(ValueError):
        assess_ltx_instance_health([], confirmed_ratio=1.5)
