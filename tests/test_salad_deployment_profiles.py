import json
from pathlib import Path


def test_salad_services_use_named_gpu_classes() -> None:
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))

    assert document["services"]["ltx25"]["resources"]["gpu_class_names"] == ["RTX 5090"]
    assert document["services"]["whisper"]["resources"]["gpu_class_names"] == ["RTX 3090"]
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
