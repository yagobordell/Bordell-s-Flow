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
    assert document["services"]["flux2_klein"]["resources"]["gpu_class_names"] == [
        "RTX 4090 (24 GB)"
    ]
    assert document["services"]["whisper"]["resources"]["gpu_class_names"] == [
        "RTX 3090 (24 GB)"
    ]
    for service in document["services"].values():
        assert "gpu_classes" not in service["resources"]


def test_salad_probe_failure_thresholds_stay_within_api_limit() -> None:
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))

    for service_name, service in document["services"].items():
        for probe_name, probe in service["probes"].items():
            threshold = probe["failure_threshold"]
            assert 1 <= threshold <= 20, f"{service_name}.{probe_name}={threshold}"

    breeze_startup = document["services"]["breeze_tts2"]["probes"]["startup"]
    assert breeze_startup["period_seconds"] * breeze_startup["failure_threshold"] == 600

    ideogram = document["services"]["ideogram4"]["probes"]
    ideogram_startup_window = (
        ideogram["startup"]["period_seconds"] * ideogram["startup"]["failure_threshold"]
    )
    ideogram_readiness_window = (
        ideogram["readiness"]["period_seconds"]
        * ideogram["readiness"]["failure_threshold"]
    )
    assert ideogram_startup_window == 600
    assert ideogram_readiness_window == 600


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


def test_ideogram_autoscaler_hard_caps_gpu_cost_during_queue_stabilization() -> None:
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = document["services"]["ideogram4"]
    autoscaler = service["autoscaler"]

    assert service["queue_name"] == "ai-video-factory-ideogram4-jobs"
    assert autoscaler["min_replicas"] == 0
    assert autoscaler["max_replicas"] == 1
    assert autoscaler["desired_queue_length"] == 1
    assert autoscaler["max_upscale_per_minute"] == 1


def test_ideogram_deployment_pins_download_and_runtime_watchdogs_and_v4_image() -> None:
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = document["services"]["ideogram4"]
    environment = service["environment"]

    assert service["image"].endswith("ideogram4-nf4-quality48-v4")
    assert service["priority"] == "high"
    assert environment["IDEOGRAM_DOWNLOAD_STALL_TIMEOUT_SECONDS"] == "600"
    assert environment["IDEOGRAM_DOWNLOAD_HARD_TIMEOUT_SECONDS"] == "1800"
    assert environment["IDEOGRAM_DOWNLOAD_POLL_SECONDS"] == "15"
    assert environment["IDEOGRAM_DOWNLOAD_MIN_MIBPS"] == "8"
    assert environment["IDEOGRAM_DOWNLOAD_THROUGHPUT_GRACE_SECONDS"] == "180"
    assert environment["IDEOGRAM_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS"] == "120"
    assert environment["IDEOGRAM_BOOTSTRAP_STALL_TIMEOUT_SECONDS"] == "720"
    assert environment["IDEOGRAM_BOOTSTRAP_HARD_TIMEOUT_SECONDS"] == "900"
    assert environment["IDEOGRAM_BOOTSTRAP_POLL_SECONDS"] == "15"
    assert environment["IDEOGRAM_BOOTSTRAP_REALLOCATE_ON_STALL"] == "true"
    assert environment["HF_HUB_DOWNLOAD_TIMEOUT"] == "120"
    assert environment["HF_HUB_ETAG_TIMEOUT"] == "30"
