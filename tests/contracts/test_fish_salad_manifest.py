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


def test_fish_smoke_verifies_reference_object_before_gpu() -> None:
    smoke = (ROOT / "scripts/smoke/run_fish_speech_smoke.py").read_text(
        encoding="utf-8"
    )
    assert "validate_reference_object(reference)" in smoke
    assert "SHA-256 mismatch" in smoke


def test_unified_compute_manager_covers_every_stack_service() -> None:
    manifest = _manifest()
    stack = (ROOT / "scripts/salad/manage_salad_stack.ps1").read_text(encoding="utf-8")

    assert "$Document.stack.service_order" in stack
    assert "job_transport" in stack
    for service_name in manifest["stack"]["service_order"]:
        assert service_name in manifest["services"]
