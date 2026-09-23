from pathlib import Path


def test_ideogram_container_patches_offline_single_file_fallback() -> None:
    dockerfile = Path("docker/workers/ideogram4/Dockerfile").read_text(encoding="utf-8")

    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "LocalEntryNotFoundError" in dockerfile
    assert '"except EntryNotFoundError:", "except (EntryNotFoundError, LocalEntryNotFoundError):", 2' in dockerfile
    assert 'raise RuntimeError("Unexpected Ideogram 4 runtime layout")' in dockerfile
