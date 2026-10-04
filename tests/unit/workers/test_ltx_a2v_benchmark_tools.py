"""Offline A2V Salad isolation and benchmark-result contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.smoke.check_ltx_a2v_benchmark_isolation import assert_quiescent_group
from scripts.smoke.check_ltx_a2v_benchmark_jobs import safe_terminal_failure_detail
from scripts.smoke.compare_ltx_a2v_benchmarks import analyze_manifest

_PROFILES = {
    "reference-bench-cpu": "ltx25-a2v-reference-bench-fp8cpu-eager-a95ab-v1",
    "reference-fp8none": "ltx25-a2v-reference-bench-fp8none-eager-a95ab-v1",
}


def test_a2v_experimental_group_is_isolated_and_not_normal_stack() -> None:
    services = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    groups = services["services"]
    exp = groups["ltx25_a2v_experimental"]
    prod = groups["ltx25"]
    i2v = groups["ltx25_i2v"]
    assert len({exp["group_name"], prod["group_name"], i2v["group_name"]}) == 3
    assert exp["environment"]["LTX_WORKER_TASK_MODE"] == "a2v_experimental"
    assert prod["environment"]["LTX_WORKER_TASK_MODE"] == "a2v"
    assert i2v["environment"]["LTX_WORKER_TASK_MODE"] == "i2v"
    assert exp["capacity"] == {"start_replicas": 1, "max_replicas": 1}
    assert exp["resources"]["gpu_class_names"] == ["RTX 5090 (32 GB)"]
    assert exp["image"] != prod["image"]
    assert "ltx25_a2v_experimental" not in services["stack"]["service_order"]


@pytest.mark.parametrize(
    ("state", "pending", "instances", "valid"),
    [
        ("stopped", False, [], True),
        ("running", False, [], False),
        ("stopped", True, [], False),
        ("stopped", False, [{"state": "running"}], False),
        ("stopped", False, [{"state": "allocating"}], False),
        ("stopped", False, [{"state": "stopped"}], True),
    ],
)
def test_benchmark_preflight_uses_actual_instance_state(
    state: str, pending: bool, instances: list[dict[str, str]], valid: bool
) -> None:
    group = {
        "name": "experimental",
        "current_state": {"status": state},
        "pending_change": pending,
        # A stopped group may retain configured capacity without live workers.
        "replicas": 1,
    }
    if valid:
        assert_quiescent_group(
            group, expected_group_name="experimental", instances=instances
        )
    else:
        with pytest.raises(RuntimeError, match="not quiescent"):
            assert_quiescent_group(
                group, expected_group_name="experimental", instances=instances
            )


def test_wrong_group_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="unexpected"):
        assert_quiescent_group(
            {
                "name": "production",
                "current_state": {"status": "stopped"},
                "pending_change": False,
            },
            expected_group_name="experimental",
        )


def _write_fake_benchmark(
    folder: Path, *, profiles: tuple[str, ...] = ("reference-bench-cpu", "reference-fp8none")
) -> Path:
    rows = []
    for profile in profiles:
        for iteration in range(1, 4):
            directory = folder / f"{profile}-{iteration}"
            directory.mkdir(parents=True)
            video = directory / "avatar_segment.mp4"
            video.write_bytes(b"fake-mp4")
            meta = directory / "metadata.json"
            meta.write_text(
                json.dumps(
                    {
                        "seed": 4242,
                        "fps": 24,
                        "width": 1280,
                        "height": 720,
                        "generation_profile": _PROFILES[profile],
                        "pipeline_reused": iteration > 1,
                        "prompt_cache_hit": iteration > 1,
                        "total_elapsed_seconds": 100 - (iteration * 5),
                        "stage1_diffusion_seconds": 50,
                        "stage2_diffusion_seconds": 12,
                        "video_decode_seconds": 7,
                        "prompt_encode_seconds": 18,
                        "cuda_peak_reserved_mb": 24000,
                    }
                ),
                encoding="utf-8",
            )
            rows.append(
                {
                    "profile": profile,
                    "iteration": iteration,
                    "video": str(video),
                    "metadata": str(meta),
                }
            )
    manifest = folder / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "profiles": list(profiles),
                "repetitions": 3,
                "seed": 4242,
                "input_audio_sha256": "a" * 64,
                "audio_sha256": "a" * 64,
                "avatar_sha256": "b" * 64,
                "pinned_image": "repo@sha256:" + "c" * 64,
                "lifecycle": {"end_to_end_seconds": 1800},
                "runs": rows,
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_comparison_uses_repeated_warm_runs_and_requires_human_review(
    tmp_path: Path,
) -> None:
    manifest = _write_fake_benchmark(tmp_path)
    summary = analyze_manifest(manifest, hash_media=False)
    control = summary["profiles"]["reference-bench-cpu"]
    assert control["cold_seconds"] == 95
    assert control["warm_samples"] == 2
    assert control["warm_median_seconds"] == 87.5
    assert control["stage1_warm_median_seconds"] == 50
    assert summary["promotion_status"] == "UNVALIDATED"
    assert "REQUIRED" in summary["human_lipsync_review"]


def test_comparison_rejects_incomplete_or_replayed_benchmark(
    tmp_path: Path,
) -> None:
    manifest = _write_fake_benchmark(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["runs"].pop()
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        analyze_manifest(manifest, hash_media=False)
    _write_fake_benchmark(tmp_path / "retest")
    document["runs"] = json.loads(
        (tmp_path / "retest" / "manifest.json").read_text(encoding="utf-8")
    )["runs"]
    document["runs"][0]["iteration"] = 2
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="labeling"):
        analyze_manifest(manifest, hash_media=False)


def test_partial_comparison_cannot_pass_without_explicit_opt_in(tmp_path: Path) -> None:
    manifest = _write_fake_benchmark(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["runs"] = [row for row in document["runs"] if row["iteration"] == 1]
    document["skipped_profiles"] = list(document["profiles"])
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="--allow-partial"):
        analyze_manifest(manifest, hash_media=False)
    report = analyze_manifest(manifest, hash_media=False, allow_partial=True)
    assert report["complete"] is False
    assert report["promotion_status"] == "UNVALIDATED"
    assert report["profiles"]["reference-bench-cpu"]["warm_median_seconds"] is None
    assert report["profiles"]["reference-fp8none"]["warm_samples"] == 0


def test_hashes_compare_to_correct_control_regardless_of_execution_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Run FP8 NONE before its control. Static must never use reference
    # guidance as its video-parity baseline.
    import scripts.smoke.compare_ltx_a2v_benchmarks as comparator

    manifest = _write_fake_benchmark(
        tmp_path,
        profiles=("reference-fp8none", "reference-bench-cpu"),
    )
    def decoded(path: Path, *, audio: bool) -> str:
        if audio:
            return "matching-original-audio"
        return (
            "reference-video"
            if path.parent.name.startswith("reference-bench-cpu")
            else "none-video"
        )

    monkeypatch.setattr(comparator, "_decoded_stream_sha", decoded)
    report = comparator.analyze_manifest(manifest, hash_media=True)
    row = report["decoded_media"][0]
    assert row["profile"] == "reference-fp8none"
    assert row["comparison_control"] == "reference-bench-cpu"
    assert row["matches_cpu_decoded_video"] is False
    assert row["matches_cpu_decoded_audio"] is True


@pytest.mark.parametrize(
    ("detail", "allowed"),
    [
        ("RuntimeError: decoder budget exceeded", True),
        (None, False),
        ("", False),
        ("NonRetryableTaskError: A2V_EXPERIMENTAL_STREAMING_STRUCTURE_INVALID", False),
    ],
)
def test_only_known_nonstructural_terminal_failure_may_be_skipped(
    detail: str | None, allowed: bool
) -> None:
    assert safe_terminal_failure_detail(detail) is allowed
