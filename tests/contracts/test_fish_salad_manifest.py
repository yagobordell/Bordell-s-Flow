import json
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())


def test_fish_manifest_is_independent_scale_to_zero_service() -> None:
    manifest = json.loads((ROOT / "deploy/salad/services.json").read_text(encoding="utf-8"))
    fish = manifest["services"]["fish_speech"]
    breeze = manifest["services"]["breeze_tts2"]

    assert fish["group_name"] == "ai-video-factory-fish-speech-worker"
    assert fish["queue_name"] == "ai-video-factory-fish-speech-jobs"
    assert fish["queue_name"] != breeze["queue_name"]
    assert fish["resources"]["gpu_class_names"] == ["RTX 4090 (24 GB)"]
    assert fish["autoscaler"]["min_replicas"] == 0
    assert fish["autoscaler"]["max_replicas"] == 1
    assert fish["probes"]["startup"]["path"] == "/health"
    assert fish["probes"]["readiness"]["path"] == "/ready"
    assert "FISH_API_KEY" not in fish["required_environment"]
    assert "FISH_API_KEY" not in fish["environment"]


def test_fish_prepare_and_control_plane_scripts_accept_service() -> None:
    for relative in (
        "scripts/salad/prepare_salad_worker_manifest.ps1",
        "scripts/salad/manage_salad_validation.ps1",
        "scripts/salad/start_salad_optimized_prewarm.ps1",
        "scripts/salad/cleanup_salad_queue.ps1",
        "scripts/salad/restore_salad_scale_to_zero.ps1",
        "scripts/salad/ensure_salad_zero_replicas.ps1",
    ):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "fish_speech" in content, relative



def test_salad_probe_failure_thresholds_fit_api_contract() -> None:
    manifest = json.loads((ROOT / "deploy/salad/services.json").read_text(encoding="utf-8"))

    for service_name, service in manifest["services"].items():
        for probe_name, probe in service["probes"].items():
            threshold = probe["failure_threshold"]
            assert 1 <= threshold <= 20, (
                f"{service_name}.{probe_name}.failure_threshold={threshold} "
                "must satisfy Salad API range 1..20"
            )



def test_fish_smoke_control_scripts_tolerate_omitted_remote_autoscaler() -> None:
    for relative in (
        "scripts/salad/start_salad_optimized_prewarm.ps1",
        "scripts/salad/start_salad_protected_smoke.ps1",
        "scripts/salad/start_salad_scale_to_zero.ps1",
        "scripts/salad/restore_salad_scale_to_zero.ps1",
    ):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "$Group.queue_autoscaler" not in content, relative
        assert 'PSObject.Properties["queue_autoscaler"]' in content, relative



def test_fish_controlled_smoke_validates_reference_before_prewarm() -> None:
    content = (ROOT / "scripts/smoke/run_fish_speech_smoke_controlled.ps1").read_text(
        encoding="utf-8"
    )
    preflight = content.index("Fish configuration preflight: before GPU allocation")
    prewarm = content.index("Fish real prewarm: one RTX 4090 replica maximum")
    assert preflight < prewarm
    assert "--preflight-only" in content



def test_phase5_fish_fallback_validates_config_before_prewarm() -> None:
    content = (ROOT / "scripts/pipeline/run_phase5_audio_controlled.ps1").read_text(
        encoding="utf-8"
    )
    preflight = content.index("Fish fallback configuration preflight: before GPU allocation")
    prewarm = content.index("Fish prewarm: exactly one ready fallback replica")
    assert preflight < prewarm
    assert "--preflight-only" in content



def test_fish_preflights_verify_reference_object_before_gpu() -> None:
    smoke = (ROOT / "scripts/smoke/run_fish_speech_smoke.py").read_text(encoding="utf-8")
    phase5 = (ROOT / "scripts/pipeline/run_phase5_audio.py").read_text(encoding="utf-8")

    assert "validate_reference_object(reference)" in smoke
    assert "_validate_reference_object(storage, reference)" in phase5
    assert "SHA-256 mismatch" in smoke
    assert "SHA-256 mismatch" in phase5



def test_phase5_breeze_prewarm_does_not_fallback_on_arbitrary_errors() -> None:
    content = (ROOT / "scripts/pipeline/run_phase5_audio_controlled.ps1").read_text(
        encoding="utf-8"
    )
    assert "Test-BreezePrewarmFallbackEligible" in content
    assert 'if (-not (Test-BreezePrewarmFallbackEligible -Message $PrewarmError))' in content
    assert "throw" in content
    assert "entered failed state during prewarm" in content
    assert "remained running but not ready during the final" in content



def test_zero_replica_guard_covers_every_stack_service() -> None:
    manifest = json.loads((ROOT / "deploy/salad/services.json").read_text(encoding="utf-8"))
    guard = (ROOT / "scripts/salad/ensure_salad_zero_replicas.ps1").read_text(encoding="utf-8")

    for service_name in manifest["stack"]["service_order"]:
        assert f'"{service_name}"' in guard, service_name



def test_unified_salad_control_plane_covers_every_stack_service() -> None:
    manifest = json.loads((ROOT / "deploy/salad/services.json").read_text(encoding="utf-8"))
    validation = (ROOT / "scripts/salad/manage_salad_validation.ps1").read_text(encoding="utf-8")
    cleanup = (ROOT / "scripts/salad/cleanup_salad_queue.ps1").read_text(encoding="utf-8")

    for service_name in manifest["stack"]["service_order"]:
        assert f'"{service_name}"' in validation, service_name
        assert f'"{service_name}"' in cleanup, service_name
