#!/usr/bin/env bash
set -Eeuo pipefail

MODEL_REPOSITORY="${LTX_MODEL_REPOSITORY:-Lightricks/LTX-2.5}"
MODEL_REVISION="${LTX_MODEL_REVISION:-main}"
MODEL_ROOT="${LTX_MODEL_ROOT:-/workspace/models/ltx-2.5}"
POLL_SECONDS="${LTX_MODEL_DOWNLOAD_PROGRESS_INTERVAL_SECONDS:-30}"
STALL_TIMEOUT_SECONDS="${LTX_MODEL_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600}"
HARD_TIMEOUT_SECONDS="${LTX_MODEL_DOWNLOAD_HARD_TIMEOUT_SECONDS:-21600}"
MIN_PROGRESS_RESET_BYTES="${LTX_MODEL_DOWNLOAD_MIN_PROGRESS_RESET_BYTES:-67108864}"
MIN_THROUGHPUT_MIBPS="${LTX_MODEL_DOWNLOAD_MIN_THROUGHPUT_MIBPS:-8}"
THROUGHPUT_GRACE_SECONDS="${LTX_MODEL_DOWNLOAD_THROUGHPUT_GRACE_SECONDS:-300}"
THROUGHPUT_WINDOW_SECONDS="${LTX_MODEL_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS:-180}"

MODEL_FILES=(
  diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
  diffusion_models/ltx-2.5-22b-dev-transformer-bf16.safetensors
  text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
  vae/ltx-2.5-video-vae-bf16.safetensors
  vae/ltx-2.5-audio-vae-bf16.safetensors
  latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
  loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors
)

mkdir -p "${MODEL_ROOT}"

needs_download=false
for model_file in "${MODEL_FILES[@]}"; do
  if [[ ! -s "${MODEL_ROOT}/${model_file}" ]]; then
    needs_download=true
    break
  fi
done
if [[ "${needs_download}" == "true" ]]; then
  /usr/local/bin/network-preflight
fi

python3 - <<'PY'
from importlib.metadata import version
import os

print(
    "HF_DOWNLOAD_RUNTIME "
    f"huggingface_hub={version('huggingface-hub')} "
    f"hf_xet={version('hf-xet')} "
    f"download_timeout={os.environ.get('HF_HUB_DOWNLOAD_TIMEOUT', '-')} "
    f"etag_timeout={os.environ.get('HF_HUB_ETAG_TIMEOUT', '-')} "
    f"xet_disabled={os.environ.get('HF_HUB_DISABLE_XET', 'false')}"
)
PY

echo "ltx25 model bootstrap repository=${MODEL_REPOSITORY} revision=${MODEL_REVISION} root=${MODEL_ROOT}"
for model_file in "${MODEL_FILES[@]}"; do
  destination="${MODEL_ROOT}/${model_file}"
  if [[ -s "${destination}" ]]; then
    echo "MODEL_PRESENT ${model_file} bytes=$(stat -c %s "${destination}")"
  else
    echo "MODEL_DOWNLOAD_START ${model_file}"
    python -m ai_video_factory.workers.download_watchdog \
      --progress-root "${MODEL_ROOT}" \
      --stall-timeout-seconds "${STALL_TIMEOUT_SECONDS}" \
      --hard-timeout-seconds "${HARD_TIMEOUT_SECONDS}" \
      --poll-seconds "${POLL_SECONDS}" \
      --min-progress-reset-bytes "${MIN_PROGRESS_RESET_BYTES}" \
      --min-throughput-mibps "${MIN_THROUGHPUT_MIBPS}" \
      --throughput-grace-seconds "${THROUGHPUT_GRACE_SECONDS}" \
      --throughput-window-seconds "${THROUGHPUT_WINDOW_SECONDS}" \
      --label "ltx25:${model_file}" \
      --reallocate-on-slow \
      -- \
      hf download "${MODEL_REPOSITORY}" "${model_file}" \
        --revision "${MODEL_REVISION}" \
        --local-dir "${MODEL_ROOT}"
    [[ -s "${destination}" ]] || {
      echo "missing model after bootstrap: ${destination}" >&2
      exit 1
    }
    echo "MODEL_DOWNLOAD_DONE ${model_file} bytes=$(stat -c %s "${destination}")"
  fi
  echo "MODEL_READY ${model_file} bytes=$(stat -c %s "${destination}")"
done
