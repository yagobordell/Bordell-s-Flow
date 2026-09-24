import json
from pathlib import Path

ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)


def _manifest() -> dict:
    return json.loads((ROOT / "deploy/salad/services.json").read_text(encoding="utf-8"))


def test_fish_manifest_is_independent_compute_service() -> None:
    manifest = _manifest()
    fish = manifest["services"]["fish_speech"]

    assert fish["group_name"] == "ai-video-factory-fish-speech-worker"
    assert fish["resources"]["gpu_class_names"] == ["RTX 4090 (24 GB)"]
    assert fish["capacity"] == {"start_replicas": 1, "max_replicas": 1}
    assert "queue_name" not in fish
    assert "autoscaler" not in fish
    assert fish["probes"]["startup"]["path"] == "/health"
    assert fish["probes"]["readiness"]["path"] == "/ready"
    assert "FISH_API_KEY" not in fish["required_environment"]
    assert "FISH_API_KEY" not in fish["environment"]


def test_salad_probe_failure_thresholds_fit_api_contract() -> None:
    manifest = _manifest()

    for service_name, service in manifest["services"].items():
        for probe_name, probe in service["probes"].items():
            threshold = probe["failure_threshold"]
            assert 1 <= threshold <= 20, (
                f"{service_name}.{probe_name}.failure_threshold={threshold} "
                "must satisfy Salad API range 1..20"
            )


def test_fish_preflights_verify_reference_object_before_gpu() -> None:
    smoke = (ROOT / "scripts/smoke/run_fish_speech_smoke.py").read_text(encoding="utf-8")
    phase5 = (ROOT / "scripts/pipeline/run_phase5_audio.py").read_text(encoding="utf-8")

    assert "validate_reference_object(reference)" in smoke
    assert "_validate_reference_object(storage, reference)" in phase5
    assert "SHA-256 mismatch" in smoke
    assert "SHA-256 mismatch" in phase5


def test_phase5_fish_fallback_validates_before_explicit_gpu_start() -> None:
    content = (ROOT / "scripts/pipeline/run_phase5_audio_controlled.ps1").read_text(
        encoding="utf-8"
    )

    preflight = content.index("Fish fallback configuration preflight: before GPU allocation")
    start = content.index('Invoke-Prewarm -Service "fish_speech"')
    assert preflight < start
    assert "--preflight-only" in content
    assert "manage_salad_worker.ps1" in content


def test_unified_compute_manager_covers_every_stack_service() -> None:
    manifest = _manifest()
    stack = (ROOT / "scripts/salad/manage_salad_stack.ps1").read_text(encoding="utf-8")

    assert "$Document.stack.service_order" in stack
    assert "job_transport" in stack
    for service_name in manifest["stack"]["service_order"]:
        assert service_name in manifest["services"]
