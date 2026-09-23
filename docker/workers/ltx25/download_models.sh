#!/usr/bin/env bash
set -Eeuo pipefail

MODEL_REPOSITORY="${LTX_MODEL_REPOSITORY:-Lightricks/LTX-2.5}"
MODEL_REVISION="${LTX_MODEL_REVISION:-main}"
MODEL_ROOT="${LTX_MODEL_ROOT:-/workspace/models/ltx-2.5}"
DOWNLOAD_PROGRESS_INTERVAL_SECONDS="${LTX_MODEL_DOWNLOAD_PROGRESS_INTERVAL_SECONDS:-30}"
DOWNLOAD_STALL_TIMEOUT_SECONDS="${LTX_MODEL_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600}"

MODEL_FILES=(
  diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
  diffusion_models/ltx-2.5-22b-dev-transformer-bf16.safetensors
  text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
  vae/ltx-2.5-video-vae-bf16.safetensors
  vae/ltx-2.5-audio-vae-bf16.safetensors
  latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
  loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors
)

require_positive_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[1-9][0-9]*$ ]]; then
    echo "${name} must be a positive integer, got '${value}'" >&2
    exit 2
  fi
}

observed_download_bytes() {
  local model_file="$1"
  python3 - "${MODEL_ROOT}" "${model_file}" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
relative = Path(sys.argv[2])
destination = root / relative
partial_dir = root / ".cache" / "huggingface" / "download" / relative.parent

total = destination.stat().st_size if destination.is_file() else 0
if partial_dir.is_dir():
    for path in partial_dir.glob("*.incomplete"):
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            pass
print(total)
PY
}

download_model_file() {
  local model_file="$1"
  local destination="${MODEL_ROOT}/${model_file}"
  local download_pid=""
  local started_epoch
  local last_activity_epoch
  local last_bytes
  local current_bytes
  local now_epoch
  local elapsed_seconds
  local idle_seconds
  local delta_bytes
  local status

  echo "MODEL_DOWNLOAD_START ${model_file}"
  hf download \
    "${MODEL_REPOSITORY}" \
    "${model_file}" \
    --revision "${MODEL_REVISION}" \
    --local-dir "${MODEL_ROOT}" &
  download_pid=$!

  started_epoch="$(date +%s)"
  last_activity_epoch="${started_epoch}"
  last_bytes="$(observed_download_bytes "${model_file}")"

  while kill -0 "${download_pid}" 2>/dev/null; do
    sleep "${DOWNLOAD_PROGRESS_INTERVAL_SECONDS}"
    now_epoch="$(date +%s)"
    current_bytes="$(observed_download_bytes "${model_file}")"
    delta_bytes=$((current_bytes - last_bytes))
    elapsed_seconds=$((now_epoch - started_epoch))

    if [[ "${current_bytes}" -ne "${last_bytes}" ]]; then
      last_activity_epoch="${now_epoch}"
      last_bytes="${current_bytes}"
    fi
    idle_seconds=$((now_epoch - last_activity_epoch))

    printf '%s\n' \
      "MODEL_DOWNLOAD_PROGRESS ${model_file} elapsed_seconds=${elapsed_seconds} observed_bytes=${current_bytes} delta_bytes=${delta_bytes} idle_seconds=${idle_seconds} stall_limit_seconds=${DOWNLOAD_STALL_TIMEOUT_SECONDS}"

    if (( idle_seconds >= DOWNLOAD_STALL_TIMEOUT_SECONDS )); then
      printf '%s\n' \
        "MODEL_DOWNLOAD_STALLED ${model_file} elapsed_seconds=${elapsed_seconds} observed_bytes=${current_bytes} idle_seconds=${idle_seconds}" >&2
      kill -TERM "${download_pid}" 2>/dev/null || true
      for _ in $(seq 1 10); do
        if ! kill -0 "${download_pid}" 2>/dev/null; then
          break
        fi
        sleep 1
      done
      kill -KILL "${download_pid}" 2>/dev/null || true
      wait "${download_pid}" 2>/dev/null || true
      return 124
    fi
  done

  set +e
  wait "${download_pid}"
  status=$?
  set -e
  if [[ "${status}" -ne 0 ]]; then
    echo "MODEL_DOWNLOAD_FAILED ${model_file} exit_code=${status}" >&2
    return "${status}"
  fi

  test -s "${destination}"
  echo "MODEL_DOWNLOAD_DONE ${model_file} bytes=$(stat -c %s "${destination}")"
}

require_positive_integer \
  "LTX_MODEL_DOWNLOAD_PROGRESS_INTERVAL_SECONDS" \
  "${DOWNLOAD_PROGRESS_INTERVAL_SECONDS}"
require_positive_integer \
  "LTX_MODEL_DOWNLOAD_STALL_TIMEOUT_SECONDS" \
  "${DOWNLOAD_STALL_TIMEOUT_SECONDS}"

mkdir -p "${MODEL_ROOT}"

python3 - <<'PY'
from importlib.metadata import version
import os

print(
    "HF_DOWNLOAD_RUNTIME "
    f"huggingface_hub={version('huggingface-hub')} "
    f"hf_xet={version('hf-xet')} "
    f"download_timeout={os.environ.get('HF_HUB_DOWNLOAD_TIMEOUT', '-')} "
    f"etag_timeout={os.environ.get('HF_HUB_ETAG_TIMEOUT', '-')} "
    f"xet_disabled={os.environ.get('HF_HUB_DISABLE_XET', 'false')} "
    f"progress_interval_seconds={os.environ.get('LTX_MODEL_DOWNLOAD_PROGRESS_INTERVAL_SECONDS', '30')} "
    f"stall_timeout_seconds={os.environ.get('LTX_MODEL_DOWNLOAD_STALL_TIMEOUT_SECONDS', '600')}"
)
PY

echo "ltx25 model bootstrap repository=${MODEL_REPOSITORY} revision=${MODEL_REVISION} root=${MODEL_ROOT}"
for model_file in "${MODEL_FILES[@]}"; do
  destination="${MODEL_ROOT}/${model_file}"
  if [[ -s "${destination}" ]]; then
    echo "MODEL_PRESENT ${model_file} bytes=$(stat -c %s "${destination}")"
    continue
  fi

  download_model_file "${model_file}"
done

python - <<'PY'
from pathlib import Path
import os

root = Path(os.environ.get("LTX_MODEL_ROOT", "/workspace/models/ltx-2.5"))
files = [
    "diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors",
    "diffusion_models/ltx-2.5-22b-dev-transformer-bf16.safetensors",
    "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
    "vae/ltx-2.5-video-vae-bf16.safetensors",
    "vae/ltx-2.5-audio-vae-bf16.safetensors",
    "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
    "loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors",
]
for relative in files:
    path = root / relative
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"missing model after bootstrap: {path}")
    print(f"MODEL_READY {relative} bytes={path.stat().st_size}")
PY
