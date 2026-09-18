from __future__ import annotations

import os
from pathlib import Path

from huggingface_hub import snapshot_download

from .model import FLUX2_KLEIN_MODEL_ID, FLUX2_KLEIN_MODEL_REVISION

_ALLOW_PATTERNS = (
    "model_index.json",
    "scheduler/**",
    "text_encoder/**",
    "tokenizer/**",
    "transformer/**",
    "vae/**",
)
_REQUIRED_PATHS = (
    "model_index.json",
    "scheduler",
    "text_encoder",
    "tokenizer",
    "transformer",
    "vae",
)


def main() -> None:
    model_root = Path(
        os.environ.get("FLUX2_KLEIN_MODEL_ROOT", "/workspace/models/flux2-klein-4b")
    )
    snapshot = model_root / "snapshot"
    repository = os.environ.get("FLUX2_KLEIN_MODEL_REPOSITORY", FLUX2_KLEIN_MODEL_ID)
    revision = os.environ.get("FLUX2_KLEIN_MODEL_REVISION", FLUX2_KLEIN_MODEL_REVISION)
    token = os.environ.get("HF_TOKEN") or None

    snapshot.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repository,
        revision=revision,
        local_dir=str(snapshot),
        token=token,
        allow_patterns=list(_ALLOW_PATTERNS),
    )

    missing = [name for name in _REQUIRED_PATHS if not (snapshot / name).exists()]
    if missing:
        raise RuntimeError(
            "FLUX.2 Klein Diffusers snapshot is incomplete; missing: " + ", ".join(missing)
        )

    forbidden = snapshot / "flux-2-klein-4b.safetensors"
    if forbidden.exists():
        raise RuntimeError(
            "FLUX.2 Klein bootstrap downloaded the duplicated single-file checkpoint"
        )

    size_bytes = sum(path.stat().st_size for path in snapshot.rglob("*") if path.is_file())
    print(
        "FLUX2_KLEIN_SNAPSHOT_COMPLETE "
        f"repository={repository} revision={revision} bytes={size_bytes}",
        flush=True,
    )


if __name__ == "__main__":
    main()
