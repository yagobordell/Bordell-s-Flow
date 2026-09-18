import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
        "scripts/prepare_salad_worker_manifest.ps1",
        "scripts/manage_salad_validation.ps1",
        "scripts/start_salad_optimized_prewarm.ps1",
        "scripts/cleanup_salad_queue.ps1",
        "scripts/restore_salad_scale_to_zero.ps1",
        "scripts/ensure_salad_zero_replicas.ps1",
    ):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "fish_speech" in content, relative
