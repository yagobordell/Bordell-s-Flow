import json
from pathlib import Path

DOCKERFILE = Path("docker/workers/flux2-klein/Dockerfile")
DOWNLOAD = Path("docker/workers/flux2-klein/download_models.sh")
ENTRYPOINT = Path("docker/workers/flux2-klein/entrypoint.sh")
MANIFEST = Path("deploy/salad/services.json")


def test_flux2_klein_download_fetches_one_diffusers_snapshot_without_duplicate_weights() -> None:
    script = DOWNLOAD.read_text(encoding="utf-8")

    for pattern in (
        '"model_index.json"',
        '"scheduler/**"',
        '"text_encoder/**"',
        '"tokenizer/**"',
        '"transformer/**"',
        '"vae/**"',
    ):
        assert pattern in script

    assert "flux-2-klein-4b.safetensors" in script
    assert "Unexpected monolithic FLUX.2 weight" in script
    assert "download_watchdog" in script
    assert "--reallocate-on-slow" in script
    assert "HF_XET_HIGH_PERFORMANCE" in script


def test_flux2_klein_image_pins_supported_diffusers_and_no_bitsandbytes() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "'diffusers==0.40.0'" in dockerfile
    assert "'transformers>=4.56.1,<5'" in dockerfile
    assert "bitsandbytes" not in dockerfile
    assert "FLUX2_KLEIN_MODEL_REVISION=e7b7dc27f91deacad38e78976d1f2b499d76a294" in dockerfile


def test_flux2_klein_health_is_independent_and_readiness_waits_for_bootstrap() -> None:
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")

    assert "start_app &" in entrypoint
    assert "wait_for_health" in entrypoint
    assert entrypoint.index("wait_for_health") < entrypoint.index("download-models")
    assert "start_bootstrap_watchdog" in entrypoint
    assert "wait_for_ready" in entrypoint


def test_flux2_klein_salad_profile_keeps_4090_and_scale_to_zero() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    service = manifest["services"]["flux2_klein"]

    assert service["group_name"] == "ai-video-factory-flux2-klein-worker"
    assert service["queue_name"] == "ai-video-factory-flux2-klein-jobs"
    assert service["dockerfile"] == "docker/workers/flux2-klein/Dockerfile"
    assert service["image"].endswith(":flux2-klein-4b-bf16-v1")
    assert service["resources"]["gpu_class_names"] == ["RTX 4090 (24 GB)"]
    assert service["resources"]["cpu"] == 8
    assert service["resources"]["memory"] == 32768
    assert service["resources"]["storage_amount"] == 103079215104
    assert service["autoscaler"]["min_replicas"] == 0
    assert service["autoscaler"]["max_replicas"] == 1
