import json
from pathlib import Path

DOCKERFILE = Path("docker/workers/flux2-klein/Dockerfile")
DOWNLOAD = Path("docker/workers/flux2-klein/download_models.sh")
MANIFEST = Path("deploy/salad/services.json")


def test_flux2_download_fetches_only_pinned_diffusers_runtime_components() -> None:
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

    assert "allow_patterns=allow_patterns" in script
    assert "flux-2-klein-4b.safetensors" not in script
    assert "snapshot_download(" in script
    assert "FLUX.2 Klein Diffusers snapshot is incomplete" in script
    assert "FLUX.2 Klein Diffusers snapshot complete" in script
    assert "download_watchdog" in script
    assert "--reallocate-on-slow" in script


def test_flux2_image_uses_native_diffusers_pipeline_without_bnb() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "'diffusers>=0.37,<0.39'" in dockerfile
    assert "Flux2KleinPipeline" in dockerfile
    assert "bitsandbytes" not in dockerfile
    assert "HF_XET_HIGH_PERFORMANCE=1" in dockerfile


def test_flux2_image_tag_and_revision_are_explicit() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    service = manifest["services"]["flux2_klein"]

    assert service["image"].endswith(":flux2-klein-4b-bf16-v1")
    assert service["environment"]["FLUX2_KLEIN_MODEL_REPOSITORY"] == (
        "black-forest-labs/FLUX.2-klein-4B"
    )
    assert service["environment"]["FLUX2_KLEIN_MODEL_REVISION"] == (
        "e7b7dc27f91deacad38e78976d1f2b499d76a294"
    )
