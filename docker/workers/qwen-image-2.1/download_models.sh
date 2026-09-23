#!/usr/bin/env bash
set -Eeuo pipefail
model_root="${QWEN_IMAGE_21_MODEL_ROOT:-/workspace/models/qwen-image-2.1}"
repository="${QWEN_IMAGE_21_MODEL_REPOSITORY:-Qwen/Qwen-Image-2.1}"
revision="${QWEN_IMAGE_21_MODEL_REVISION:-b3179ad355be050328e483a9dfdd9e60cd62adfa}"
snapshot="${model_root}/snapshot"
marker="${model_root}/.ready"
expected="${repository}@${revision}"
required_files=(
  "model_index.json"
  "processor/tokenizer.json"
  "scheduler/scheduler_config.json"
  "text_encoder/model.safetensors.index.json"
  "transformer/diffusion_pytorch_model.safetensors.index.json"
  "vae/diffusion_pytorch_model.safetensors"
)

snapshot_ready() {
  local relative
  for relative in "${required_files[@]}"; do
    [[ -s "${snapshot}/${relative}" ]] || return 1
  done
}

if [[ -f "${marker}" ]] && [[ "$(cat "${marker}")" == "${expected}" ]] && snapshot_ready; then
  echo "Qwen-Image-2.1 model bootstrap cache hit"
  exit 0
fi
rm -f "${marker}"
rm -rf "${snapshot}"
mkdir -p "${snapshot}"
HF_HUB_OFFLINE=0 python - <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id=os.environ.get("QWEN_IMAGE_21_MODEL_REPOSITORY", "Qwen/Qwen-Image-2.1"),
    revision=os.environ.get("QWEN_IMAGE_21_MODEL_REVISION", "b3179ad355be050328e483a9dfdd9e60cd62adfa"),
    local_dir=os.path.join(os.environ.get("QWEN_IMAGE_21_MODEL_ROOT", "/workspace/models/qwen-image-2.1"), "snapshot"),
    token=os.environ.get("HF_TOKEN"),
)
PY
if ! snapshot_ready; then
  echo "Qwen-Image-2.1 model bootstrap completed with missing required files" >&2
  exit 1
fi
printf '%s\n' "${expected}" > "${marker}"
echo "Qwen-Image-2.1 model bootstrap complete"
