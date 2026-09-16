#!/usr/bin/env bash
set -Eeuo pipefail

model_root="${WHISPER_MODEL_ROOT:-/workspace/models/whisper-large-v3-turbo}"
repository="${WHISPER_MODEL_REPOSITORY:-openai/whisper-large-v3-turbo}"
revision="${WHISPER_MODEL_REVISION:-main}"
staging_root="${model_root}.staging"
stall_timeout="${WHISPER_DOWNLOAD_STALL_TIMEOUT_SECONDS:-180}"
hard_timeout="${WHISPER_DOWNLOAD_HARD_TIMEOUT_SECONDS:-600}"
poll_seconds="${WHISPER_DOWNLOAD_POLL_SECONDS:-10}"
min_mibps="${WHISPER_DOWNLOAD_MIN_MIBPS:-4}"
throughput_grace="${WHISPER_DOWNLOAD_THROUGHPUT_GRACE_SECONDS:-90}"
throughput_window="${WHISPER_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS:-60}"

export HF_HUB_DOWNLOAD_TIMEOUT="${HF_HUB_DOWNLOAD_TIMEOUT:-120}"
export HF_HUB_ETAG_TIMEOUT="${HF_HUB_ETAG_TIMEOUT:-30}"

mkdir -p "${model_root}"

if [[ -s "${model_root}/config.json" ]] && \
   find "${model_root}" -type f -name '*.safetensors' -size +0c | grep -q .; then
  echo "Whisper model already present at ${model_root}"
  exit 0
fi

rm -rf "${staging_root}"
mkdir -p "${staging_root}"

python3 - <<'PY'
from importlib.metadata import PackageNotFoundError, version

for package in ("huggingface-hub", "hf-xet"):
    try:
        print(f"HF_DOWNLOAD_RUNTIME {package}={version(package)}")
    except PackageNotFoundError:
        print(f"HF_DOWNLOAD_RUNTIME {package}=missing")
PY

echo "Downloading ${repository}@${revision} to staging directory ${staging_root}"
python -m ai_video_factory.workers.download_watchdog \
  --progress-root "${staging_root}" \
  --stall-timeout-seconds "${stall_timeout}" \
  --hard-timeout-seconds "${hard_timeout}" \
  --poll-seconds "${poll_seconds}" \
  --label whisper \
  --min-throughput-mibps "${min_mibps}" \
  --throughput-grace-seconds "${throughput_grace}" \
  --throughput-window-seconds "${throughput_window}" \
  --reallocate-on-slow \
  -- \
  hf download "${repository}" \
    --revision "${revision}" \
    --local-dir "${staging_root}"

if [[ ! -s "${staging_root}/config.json" ]]; then
  echo "Whisper config.json was not downloaded" >&2
  exit 1
fi
if ! find "${staging_root}" -type f -name '*.safetensors' -size +0c | grep -q .; then
  echo "Whisper safetensors weights were not downloaded" >&2
  exit 1
fi

rm -rf "${model_root}"
mv "${staging_root}" "${model_root}"

echo "Whisper model bootstrap complete"
