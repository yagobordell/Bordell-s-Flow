#!/usr/bin/env bash
set -Eeuo pipefail

BOOTSTRAP_STARTED_EPOCH="$(date +%s)"

MANIFEST_PATH="${LTX_MODEL_MANIFEST:-/usr/local/share/ltx25-model-manifest.json}"
MODEL_ROOT="${LTX_MODEL_ROOT:-/workspace/models/ltx-2.5}"
MODEL_REPOSITORY="${LTX_MODEL_REPOSITORY:-Lightricks/LTX-2.5}"
MODEL_REVISION="${LTX_MODEL_REVISION:-6c7e5e573ac1667efc83407806fe9b0b93730e60}"
INCLUDE_DEV_ASSETS="${LTX_INCLUDE_A2V_DEV_ASSETS:-false}"
INCLUDE_I2V_NVFP4_ASSETS="${LTX_INCLUDE_I2V_NVFP4_ASSETS:-false}"
INSTALLED_MANIFEST="${MODEL_ROOT}/.bordell-installed-model-manifest.json"
# Only the isolated A2V experimental Salad service sets this. Zero preserves
# production LTX startup and all other worker modes unchanged.
A2V_STORAGE_MIN_MIBPS="${LTX_A2V_EXPERIMENTAL_BOOTSTRAP_MIN_STORAGE_MIBPS:-0}"
A2V_TRANSFORMER_FILE="diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors"

POLL_SECONDS="${LTX_MODEL_DOWNLOAD_PROGRESS_INTERVAL_SECONDS:-30}"
STALL_TIMEOUT_SECONDS="${LTX_MODEL_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600}"
HARD_TIMEOUT_SECONDS="${LTX_MODEL_DOWNLOAD_HARD_TIMEOUT_SECONDS:-21600}"
MIN_PROGRESS_RESET_BYTES="${LTX_MODEL_DOWNLOAD_MIN_PROGRESS_RESET_BYTES:-67108864}"
MIN_THROUGHPUT_MIBPS="${LTX_MODEL_DOWNLOAD_MIN_THROUGHPUT_MIBPS:-8}"
THROUGHPUT_GRACE_SECONDS="${LTX_MODEL_DOWNLOAD_THROUGHPUT_GRACE_SECONDS:-300}"
THROUGHPUT_WINDOW_SECONDS="${LTX_MODEL_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS:-180}"

[[ -r "${MANIFEST_PATH}" ]] || {
  echo "missing LTX model manifest: ${MANIFEST_PATH}" >&2
  exit 1
}

mapfile -t MANIFEST_VALUES < <(
  python3 - "${MANIFEST_PATH}" "${INCLUDE_DEV_ASSETS}" "${INCLUDE_I2V_NVFP4_ASSETS}" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
include_dev = sys.argv[2].strip().lower() in {"1", "true", "yes", "on"}
include_nvfp4 = sys.argv[3].strip().lower() in {"1", "true", "yes", "on"}
print(manifest["repository"])
print(manifest["revision"])
for item in manifest["shared_files"]:
    print("FILE\t" + item)
if include_dev:
    for item in manifest["dev_files"]:
        print("FILE\t" + item)
if include_nvfp4:
    for item in manifest["i2v_nvfp4_files"]:
        print("FILE\t" + item)
PY
)

EXPECTED_REPOSITORY="${MANIFEST_VALUES[0]}"
EXPECTED_REVISION="${MANIFEST_VALUES[1]}"
MODEL_FILES=()
for value in "${MANIFEST_VALUES[@]:2}"; do
  [[ "${value}" == FILE$'\t'* ]] || continue
  MODEL_FILES+=("${value#FILE$'\t'}")
done

if [[ "${MODEL_REPOSITORY}" != "${EXPECTED_REPOSITORY}" ]]; then
  echo "LTX model repository does not match pinned manifest: ${MODEL_REPOSITORY} != ${EXPECTED_REPOSITORY}" >&2
  exit 1
fi
if [[ "${MODEL_REVISION}" != "${EXPECTED_REVISION}" ]]; then
  echo "LTX model revision does not match pinned manifest: ${MODEL_REVISION} != ${EXPECTED_REVISION}" >&2
  exit 1
fi

mkdir -p "${MODEL_ROOT}"

manifest_is_valid=false
if python -m ai_video_factory.workers.ltx25.model_manifest validate \
  --installed-path "${INSTALLED_MANIFEST}" \
  --repository "${MODEL_REPOSITORY}" \
  --revision "${MODEL_REVISION}" \
  --root "${MODEL_ROOT}" \
  "${MODEL_FILES[@]}"
then
  manifest_is_valid=true
fi

if [[ "${manifest_is_valid}" == "false" ]]; then
  # An outdated manifest must not make the HTTP worker ready while assets download.
  rm -f "${INSTALLED_MANIFEST}"
  # Screen the ACTUAL model filesystem before downloading 71 GB. The scratch
  # sample is an admission hint, not a replacement for the real checkpoint
  # test below. Reject/reallocate before model downloads if clearly too slow.
  if [[ "${A2V_STORAGE_MIN_MIBPS}" != "0" ]]; then
    python -m ai_video_factory.workers.ltx25.checkpoint_io \
      --bootstrap-root "${MODEL_ROOT}" \
      --minimum-mibps "${A2V_STORAGE_MIN_MIBPS}" --reallocate-on-slow
  fi
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

echo "ltx25 model bootstrap repository=${MODEL_REPOSITORY} revision=${MODEL_REVISION} root=${MODEL_ROOT} include_dev=${INCLUDE_DEV_ASSETS}"

if [[ "${manifest_is_valid}" == "true" ]]; then
  echo "MODEL_MANIFEST_VALID path=${INSTALLED_MANIFEST}"
else
  for model_file in "${MODEL_FILES[@]}"; do
    destination="${MODEL_ROOT}/${model_file}"
    echo "MODEL_VERIFY_START ${model_file}"
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
      echo "missing model after pinned bootstrap: ${destination}" >&2
      exit 1
    }
    echo "MODEL_VERIFY_DONE ${model_file} bytes=$(stat -c %s "${destination}")"
    # The downloaded transformer is authoritative for actual inference I/O.
    # Reject a marginal host BEFORE downloading Gemma and the other files.
    if [[ "${A2V_STORAGE_MIN_MIBPS}" != "0" && "${model_file}" == "${A2V_TRANSFORMER_FILE}" ]]; then
      python -m ai_video_factory.workers.ltx25.checkpoint_io \
        --checkpoint "${destination}" \
        --minimum-mibps "${A2V_STORAGE_MIN_MIBPS}" --reallocate-on-slow
    fi
  done

  python -m ai_video_factory.workers.ltx25.model_manifest write \
    --installed-path "${INSTALLED_MANIFEST}" \
    --repository "${MODEL_REPOSITORY}" \
    --revision "${MODEL_REVISION}" \
    --root "${MODEL_ROOT}" \
    "${MODEL_FILES[@]}"
fi

for model_file in "${MODEL_FILES[@]}"; do
  destination="${MODEL_ROOT}/${model_file}"
  [[ -s "${destination}" ]] || {
    echo "model missing after manifest validation: ${destination}" >&2
    exit 1
  }
  echo "MODEL_READY ${model_file} bytes=$(stat -c %s "${destination}")"
done

BOOTSTRAP_FINISHED_EPOCH="$(date +%s)"
echo "LTX25_MODEL_BOOTSTRAP_METRIC cache_hit=${manifest_is_valid} files=${#MODEL_FILES[@]} elapsed_seconds=$((BOOTSTRAP_FINISHED_EPOCH - BOOTSTRAP_STARTED_EPOCH))"

if [[ "${manifest_is_valid}" == "true" && "${A2V_STORAGE_MIN_MIBPS}" != "0" ]]; then
  # Existing cache has not necessarily been proven fast in this container.
  python -m ai_video_factory.workers.ltx25.checkpoint_io \
    --checkpoint "${MODEL_ROOT}/${A2V_TRANSFORMER_FILE}" \
    --minimum-mibps "${A2V_STORAGE_MIN_MIBPS}" --reallocate-on-slow
fi

if [[ -n "${LTX_RUNTIME_BOOTSTRAP_MARKER:-}" ]]; then
  marker_dir="$(dirname "${LTX_RUNTIME_BOOTSTRAP_MARKER}")"
  mkdir -p "${marker_dir}"
  marker_tmp="${LTX_RUNTIME_BOOTSTRAP_MARKER}.tmp.$$"
  printf '%s\n' "${MODEL_REPOSITORY}@${MODEL_REVISION}" > "${marker_tmp}"
  mv -f "${marker_tmp}" "${LTX_RUNTIME_BOOTSTRAP_MARKER}"
  echo "LTX25_RUNTIME_BOOTSTRAP_READY marker=${LTX_RUNTIME_BOOTSTRAP_MARKER}"
fi
