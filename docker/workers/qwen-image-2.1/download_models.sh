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

/usr/local/bin/network-preflight

rm -f "${marker}"
rm -rf "${snapshot}"
mkdir -p "${snapshot}"

download_args=(download "${repository}" --revision "${revision}" --local-dir "${snapshot}")
if [[ -n "${HF_TOKEN:-}" ]]; then
  download_args+=(--token "${HF_TOKEN}")
fi

HF_HUB_OFFLINE=0 python -m ai_video_factory.workers.download_watchdog \
  --progress-root "${snapshot}" \
  --stall-timeout-seconds "${QWEN_IMAGE_21_DOWNLOAD_STALL_TIMEOUT_SECONDS:-720}" \
  --hard-timeout-seconds "${QWEN_IMAGE_21_DOWNLOAD_HARD_TIMEOUT_SECONDS:-7200}" \
  --poll-seconds "${QWEN_IMAGE_21_DOWNLOAD_POLL_SECONDS:-15}" \
  --min-progress-reset-bytes "${QWEN_IMAGE_21_DOWNLOAD_MIN_PROGRESS_RESET_BYTES:-67108864}" \
  --min-throughput-mibps "${QWEN_IMAGE_21_DOWNLOAD_MIN_THROUGHPUT_MIBPS:-8}" \
  --throughput-grace-seconds "${QWEN_IMAGE_21_DOWNLOAD_THROUGHPUT_GRACE_SECONDS:-300}" \
  --throughput-window-seconds "${QWEN_IMAGE_21_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS:-180}" \
  --label qwen-image-2.1 \
  --reallocate-on-slow \
  -- \
  hf "${download_args[@]}"

if ! snapshot_ready; then
  echo "Qwen-Image-2.1 model bootstrap completed with missing required files" >&2
  exit 1
fi

printf '%s\n' "${expected}" > "${marker}"
echo "Qwen-Image-2.1 model bootstrap complete"
