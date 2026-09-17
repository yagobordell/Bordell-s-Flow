import json
from pathlib import Path

DOWNLOAD = Path("docker/workers/flux-schnell/download_models.sh")
MANIFEST = Path("deploy/salad/services.json")


def test_flux_download_only_fetches_diffusers_runtime_components() -> None:
    script = DOWNLOAD.read_text(encoding="utf-8")

    for pattern in (
        '"model_index.json"',
        '"scheduler/**"',
        '"text_encoder/**"',
        '"text_encoder_2/**"',
        '"tokenizer/**"',
        '"tokenizer_2/**"',
        '"transformer/**"',
        '"vae/**"',
    ):
        assert pattern in script

    assert "allow_patterns=allow_patterns" in script
    assert "flux1-schnell.safetensors" not in script
    assert "ae.safetensors" not in script
    assert "FLUX Diffusers snapshot is incomplete" in script
    assert "FLUX Schnell Diffusers snapshot complete" in script


def test_flux_image_tag_versions_cold_start_change() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["services"]["flux_schnell"]["image"].endswith(
        ":flux1-schnell-bnb4-v2"
    )
