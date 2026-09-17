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

python - <<'PY'
import os
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id=os.environ.get("FLUX_MODEL_REPOSITORY", "black-forest-labs/FLUX.1-schnell"),
    revision=os.environ.get("FLUX_MODEL_REVISION", "main"),
    local_dir=os.path.join(os.environ.get("FLUX_MODEL_ROOT", "/workspace/models/flux-schnell"), "snapshot"),
    token=os.environ["HF_TOKEN"],
)
PY

printf '%s\n' "${repository}@${revision}" > "${marker}"
echo "FLUX Schnell model bootstrap complete"
