import json
from pathlib import Path

DOCKERFILE = Path("docker/workers/flux2-klein/Dockerfile")
DOWNLOAD = Path("docker/workers/flux2-klein/download_models.sh")
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


def test_flux_image_includes_tokenizer_runtime_dependencies() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "'sentencepiece>=0.2,<0.3'" in dockerfile
    assert "'protobuf>=5,<7'" in dockerfile
    assert "import bitsandbytes, diffusers, google.protobuf, sentencepiece" in dockerfile


def test_flux_image_tag_versions_tokenizer_runtime_change() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["services"]["flux2_klein"]["image"].endswith(
        ":flux2-klein-4b-bf16-v1"
    )
