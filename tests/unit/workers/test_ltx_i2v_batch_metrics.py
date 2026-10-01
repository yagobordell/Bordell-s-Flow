"""Offline tests for the single-GPU LTX I2V batch metrics contract."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.smoke import ltx25_i2v_benchmark_metrics as benchmark

JOB_ID = "phase8-shot-001-fake"
PROFILE = "cfg-b2"


def _phase_event(*, profile: str = PROFILE) -> dict[str, str]:
    return {
        "time": "2026-10-01T19:00:30Z",
        "text_log": (
            "INFO:ltx:LTX25_I2V_PHASE_METRICS "
            + json.dumps(
                {
                    "generation_profile": benchmark.PROFILES[profile],
                    "stage1_total_seconds": 42.4,
                    "stage2_total_seconds": 60.0,
                    "prompt_encode_seconds": 38.0,
                    "video_decode_seconds": 3.1,
                    "audio_decode_seconds": 0.0,
                    "cuda_peak_reserved_mb": 9000.0,
                    "total_generation_seconds": 152.0,
                }
            )
        ),
    }


def _task_event() -> dict[str, str]:
    return {
        "time": "2026-10-01T19:00:32Z",
        "text_log": (
            "INFO:worker:INFERENCE_JOB_TASK_DONE task=video.ltx25.generate "
            f"job_id={JOB_ID} attempt=1 task_seconds=152.010"
        ),
    }


def test_full_phase_metrics_and_exact_completed_job_are_retained() -> None:
    result = benchmark._extract_metrics(
        [_phase_event(), _task_event()], profile=PROFILE, job_id=JOB_ID
    )
    assert result["profile"] == PROFILE
    assert result["job_id"] == JOB_ID
    assert result["task_seconds"] == 152.010
    assert result["metrics"]["stage1_total_seconds"] == 42.4
    assert result["metrics"]["cuda_peak_reserved_mb"] == 9000.0
    assert len(result["events"]) == 2


def test_missing_duplicate_or_wrong_profile_never_produces_fabricated_times() -> None:
    with pytest.raises(ValueError, match="found 0"):
        benchmark._extract_metrics(
            [_phase_event(profile="default"), _task_event()],
            profile=PROFILE,
            job_id=JOB_ID,
        )
    with pytest.raises(ValueError, match="found 2"):
        benchmark._extract_metrics(
            [_phase_event(), _phase_event(), _task_event()],
            profile=PROFILE,
            job_id=JOB_ID,
        )
    with pytest.raises(ValueError, match="TASK_DONE"):
        benchmark._extract_metrics(
            [_phase_event(), _task_event()],
            profile=PROFILE,
            job_id="wrong-job",
        )


def test_exported_baseline_csv_is_parsed_without_external_services(tmp_path: Path) -> None:
    path = tmp_path / "baseline.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Time", "Text Log"])
        writer.writeheader()
        for event in (_phase_event(profile="default"), _task_event()):
            writer.writerow({"Time": event["time"], "Text Log": event["text_log"]})
    events = benchmark._import_csv(path)
    result = benchmark._extract_metrics(events, profile="default", job_id=JOB_ID)
    assert result["metrics"]["total_generation_seconds"] == 152.0


def test_salad_log_collection_uses_bounded_windows_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, datetime, datetime]] = []

    def fake_query(**kwargs: object) -> list[dict[str, str]]:
        calls.append((kwargs["marker"], kwargs["start"], kwargs["end"]))
        if kwargs["marker"] == benchmark.MARKERS[0]:
            return [_phase_event()]
        return [_task_event()]

    monkeypatch.setattr(benchmark, "_log_query", fake_query)
    start = datetime(2026, 10, 1, 19, tzinfo=timezone.utc)
    events = benchmark._download_log_events(
        start=start,
        end=start + timedelta(minutes=11),
        organization="test",
        project="test",
        key="test",
        group="test",
    )
    assert len(calls) == 6  # 3 windows for each marker, not one truncated API page.
    assert len(events) == 2
    assert calls[0][2] - calls[0][1] == timedelta(minutes=5)
    assert calls[2][2] - calls[2][1] == timedelta(minutes=1)


def test_batch_csv_preserves_all_dynamic_model_phase_columns(tmp_path: Path) -> None:
    run = tmp_path / "cfg-b2"
    run.mkdir()
    result = {
        "profile": "cfg-b2",
        "status": "succeeded",
        "run_id": "batch-A",
        "job_id": JOB_ID,
        "client_wall_seconds": 160.0,
        "video_sha256": "a" * 64,
    }
    (run / "run-result.json").write_text(json.dumps(result), encoding="utf-8")
    metrics = benchmark._extract_metrics(
        [_phase_event(), _task_event()], profile=PROFILE, job_id=JOB_ID
    )
    metrics["source"] = "salad-api"
    (run / "phase-metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    benchmark._summarize(tmp_path)
    with (tmp_path / "summary.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["stage1_total_seconds"] == "42.4"
    assert rows[0]["prompt_encode_seconds"] == "38.0"
    assert rows[0]["cuda_peak_reserved_mb"] == "9000.0"
    assert rows[0]["client_wall_seconds"] == "160.0"
    assert rows[0]["metrics_source"] == "salad-api"


def test_salad_api_collection_uses_env_selected_account_without_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []

    def fake_logs(**kwargs: object) -> list[dict[str, str]]:
        captured.append(kwargs)
        return [_phase_event(), _task_event()]

    monkeypatch.setattr(benchmark, "_load_environment", lambda _: (
        "correct-org", "correct-project", "fake-key", "isolated-i2v"
    ))
    monkeypatch.setattr(benchmark, "_download_log_events", fake_logs)
    output = tmp_path / "metrics.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ltx25_i2v_benchmark_metrics.py",
            "collect",
            "--profile", PROFILE,
            "--job-id", JOB_ID,
            "--start-utc", "2026-10-01T18:55:00Z",
            "--end-utc", "2026-10-01T19:05:00Z",
            "--output", str(output),
        ],
    )
    assert benchmark.main() == 0
    assert captured[0]["organization"] == "correct-org"
    assert captured[0]["project"] == "correct-project"
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8"))["task_seconds"] == 152.010


def test_batch_wrapper_keeps_one_capacity_session_and_saves_every_run() -> None:
    root = Path(__file__).resolve().parents[3]
    wrapper = (
        root / "scripts" / "smoke" / "run_ltx25_i2v_benchmark_batch.ps1"
    ).read_text(encoding="utf-8")
    assert wrapper.count("-Action Start -Service ltx25_i2v") == 1
    assert wrapper.count("-Action Stop -Service ltx25_i2v") == 1
    assert wrapper.index("-Action Start -Service ltx25_i2v") < wrapper.index(
        "foreach ($Profile in $Profiles) {", wrapper.index("$StartRequested = $false")
    )
    assert wrapper.index("-Action Stop -Service ltx25_i2v") > wrapper.index(
        "foreach ($Profile in $Profiles) {", wrapper.index("$StartRequested = $false")
    )
    assert "--no-retry-terminal" in wrapper
    assert '"phase-metrics.json"' in wrapper
    assert "Assert-SameInputs" in wrapper
    assert "Saved complete times" not in wrapper  # no unsupported success message
