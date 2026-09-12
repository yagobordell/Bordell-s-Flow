from pathlib import Path


def test_ideogram_container_uses_downloaded_local_snapshot() -> None:
    dockerfile = Path("docker/workers/ideogram4/Dockerfile").read_text(encoding="utf-8")
    downloader = Path("docker/workers/ideogram4/download_models.sh").read_text(
        encoding="utf-8"
    )

    assert (
        "IDEOGRAM_MODEL_LOCAL_SNAPSHOT=/workspace/models/ideogram4/snapshot"
        in dockerfile
    )
    assert 'hub_call = "hf_hub_download("' in dockerfile
    assert "text.count(hub_call) != 6" in dockerfile
    assert 'text = text.replace(hub_call, "_resolve_model_file(")' in dockerfile
    assert "def _resolve_model_file(repo_id: str, filename: str) -> str:" in dockerfile
    assert "IDEOGRAM_MODEL_LOCAL_SNAPSHOT" in dockerfile
    assert "config.weights_repo = str(local_snapshot)" in dockerfile

    assert 'snapshot_link="${model_root}/snapshot"' in downloader
    assert 'ln -s "${snapshot_path}" "${snapshot_link}"' in downloader
    assert "transformer/diffusion_pytorch_model.safetensors" in downloader
    assert "unconditional_transformer/diffusion_pytorch_model.safetensors" in downloader
    assert downloader.index('ln -s "${snapshot_path}" "${snapshot_link}"') < downloader.index(
        'printf \'%s@%s\\n\' "${repo}" "${revision}" > "${model_root}/.ready"'
    )
