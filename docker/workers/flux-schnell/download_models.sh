#!/usr/bin/env bash
set -Eeuo pipefail

model_root="${FLUX_MODEL_ROOT:-/workspace/models/flux-schnell}"
repository="${FLUX_MODEL_REPOSITORY:-black-forest-labs/FLUX.1-schnell}"
revision="${FLUX_MODEL_REVISION:-main}"
snapshot="${model_root}/snapshot"
marker="${model_root}/.ready"

if [[ -f "${marker}" ]] && [[ "$(cat "${marker}")" == "${repository}@${revision}" ]] && [[ -d "${snapshot}" ]]; then
  echo "FLUX Schnell model snapshot already prepared"
  exit 0
fi

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "HF_TOKEN is required to download the gated FLUX.1-schnell repository" >&2
  exit 1
fi

rm -rf "${snapshot}"
mkdir -p "${snapshot}"

echo "FLUX Schnell bootstrap: downloading only Diffusers runtime components"
HF_HUB_OFFLINE=0 python - <<'PY'
import os
from pathlib import Path

from huggingface_hub import snapshot_download

model_root = Path(os.environ.get("FLUX_MODEL_ROOT", "/workspace/models/flux-schnell"))
snapshot = model_root / "snapshot"
allow_patterns = [
    "model_index.json",
    "scheduler/**",
    "text_encoder/**",
    "text_encoder_2/**",
    "tokenizer/**",
    "tokenizer_2/**",
    "transformer/**",
    "vae/**",
]

snapshot_download(
    repo_id=os.environ.get("FLUX_MODEL_REPOSITORY", "black-forest-labs/FLUX.1-schnell"),
    revision=os.environ.get("FLUX_MODEL_REVISION", "main"),
    local_dir=str(snapshot),
    token=os.environ["HF_TOKEN"],
    allow_patterns=allow_patterns,
)

required = [
    snapshot / "model_index.json",
    snapshot / "scheduler",
    snapshot / "text_encoder",
    snapshot / "text_encoder_2",
    snapshot / "tokenizer",
    snapshot / "tokenizer_2",
    snapshot / "transformer",
    snapshot / "vae",
]
missing = [str(path.relative_to(snapshot)) for path in required if not path.exists()]
if missing:
    raise RuntimeError(f"FLUX Diffusers snapshot is incomplete; missing: {', '.join(missing)}")

size_bytes = sum(path.stat().st_size for path in snapshot.rglob("*") if path.is_file())
print(f"FLUX Schnell Diffusers snapshot complete: bytes={size_bytes}", flush=True)
PY

printf '%s\n' "${repository}@${revision}" > "${marker}"
echo "FLUX Schnell model bootstrap complete"
