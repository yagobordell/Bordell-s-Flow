import json
from pathlib import Path

DOCKERFILE = Path("docker/workers/flux2-klein/Dockerfile")
DOWNLOAD = Path("docker/workers/flux2-klein/download_models.sh")
BOOTSTRAP = Path("src/ai_video_factory/workers/flux2_klein/bootstrap.py")
MANIFEST = Path("deploy/salad/services.json")


def test_flux2_download_only_fetches_diffusers_runtime_components_once() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    for pattern in (
        '"model_index.json"',
        '"scheduler/**"',
        '"text_encoder/**"',
        '"tokenizer/**"',
        '"transformer/**"',
        '"vae/**"',
    ):
        assert pattern in script

    assert "allow_patterns=list(_ALLOW_PATTERNS)" in script
    assert '"flux-2-klein-4b.safetensors"' in script
    assert "duplicated single-file checkpoint" in script
    assert "text_encoder_2" not in script
    assert "tokenizer_2" not in script


def test_flux2_download_uses_shared_watchdog_xet_and_reallocation() -> None:
    script = DOWNLOAD.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "ai_video_factory.workers.download_watchdog" in script
    assert "--reallocate-on-slow" in script
    assert "FLUX2_KLEIN_DOWNLOAD_HARD_TIMEOUT_SECONDS" in script
    assert "FLUX2_KLEIN_DOWNLOAD_MIN_MIBPS" in script
    assert "HF_XET_HIGH_PERFORMANCE=1" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile


def test_flux2_image_pins_stable_diffusers_runtime_without_bitsandbytes() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "'diffusers==0.40.0'" in dockerfile
    assert "'transformers==5.17.0'" in dockerfile
    assert "from diffusers import Flux2KleinPipeline" in dockerfile
    assert "bitsandbytes" not in dockerfile


def test_flux2_manifest_uses_new_image_and_public_pinned_model() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    service = manifest["services"]["flux2_klein"]

    assert service["image"].endswith(":flux2-klein-4b-bf16-diffusers040-v1")
    assert service["required_environment"] == []
    assert service["environment"]["FLUX2_KLEIN_MODEL_REPOSITORY"] == (
        "black-forest-labs/FLUX.2-klein-4B"
    )
    assert service["environment"]["FLUX2_KLEIN_MODEL_REVISION"] == (
        "e7b7dc27f91deacad38e78976d1f2b499d76a294"
    )
