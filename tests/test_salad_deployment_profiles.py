import json
from pathlib import Path


def test_salad_services_use_named_gpu_classes() -> None:
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))

    assert document["services"]["ltx25"]["resources"]["gpu_class_names"] == [
        "RTX 5090 (32 GB)"
    ]
    assert document["services"]["breeze_tts2"]["resources"]["gpu_class_names"] == [
        "RTX 4090 (24 GB)"
    ]
    assert document["services"]["ideogram4"]["resources"]["gpu_class_names"] == [
        "RTX 4090 (24 GB)"
    ]
    assert document["services"]["whisper"]["resources"]["gpu_class_names"] == [
        "RTX 3090 (24 GB)"
    ]
    for service in document["services"].values():
        assert "gpu_classes" not in service["resources"]


def test_salad_manager_resolves_gpu_names_through_organization_api() -> None:
    script = Path("scripts/manage_salad_worker.ps1").read_text(encoding="utf-8")

    assert '"$OrganizationApiBase/gpu-classes"' in script
    assert "function Resolve-GpuClassIds" in script
    assert 'PSObject.Properties["gpu_class_names"]' in script
    assert "$GpuClassIds = @(Resolve-GpuClassIds -Headers $Headers)" in script
    assert "gpu_classes = $GpuClassIds" in script


def test_ltx_autoscaler_allows_parallel_shot_workers() -> None:
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    autoscaler = document["services"]["ltx25"]["autoscaler"]

    assert autoscaler["min_replicas"] == 0
    assert autoscaler["max_replicas"] == 4
    assert autoscaler["desired_queue_length"] == 1
    assert autoscaler["max_upscale_per_minute"] == 2


def test_breeze_autoscaler_allows_parallel_narration_workers() -> None:
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    autoscaler = document["services"]["breeze_tts2"]["autoscaler"]

    assert autoscaler["min_replicas"] == 0
    assert autoscaler["max_replicas"] == 2
    assert autoscaler["desired_queue_length"] == 1


def test_ideogram_autoscaler_allows_parallel_reference_and_keyframe_workers() -> None:
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = document["services"]["ideogram4"]
    autoscaler = service["autoscaler"]

    assert service["queue_name"] == "ai-video-factory-ideogram4-jobs"
    assert autoscaler["min_replicas"] == 0
    assert autoscaler["max_replicas"] == 4
    assert autoscaler["desired_queue_length"] == 1
    assert autoscaler["max_upscale_per_minute"] == 2
