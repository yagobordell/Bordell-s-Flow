#!/usr/bin/env bash
set -Eeuo pipefail

repository="${FLUX2_KLEIN_MODEL_REPOSITORY:-black-forest-labs/FLUX.2-klein-4B}"
revision="${FLUX2_KLEIN_MODEL_REVISION:-e7b7dc27f91deacad38e78976d1f2b499d76a294}"
model_root="${FLUX2_KLEIN_MODEL_ROOT:-/workspace/models/flux2-klein-4b}"
snapshot="${model_root}/snapshot"
marker="${model_root}/.ready"
stall_timeout="${FLUX2_KLEIN_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600}"
hard_timeout="${FLUX2_KLEIN_DOWNLOAD_HARD_TIMEOUT_SECONDS:-1800}"
poll_seconds="${FLUX2_KLEIN_DOWNLOAD_POLL_SECONDS:-15}"
min_mibps="${FLUX2_KLEIN_DOWNLOAD_MIN_MIBPS:-6}"
throughput_grace="${FLUX2_KLEIN_DOWNLOAD_THROUGHPUT_GRACE_SECONDS:-180}"
throughput_window="${FLUX2_KLEIN_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS:-120}"

expected_marker="${repository}@${revision}"
if [[ -f "${marker}" ]] && [[ "$(cat "${marker}")" == "${expected_marker}" ]] && [[ -d "${snapshot}" ]]; then
    echo "FLUX.2 Klein model snapshot already prepared: ${expected_marker}"
    exit 0
fi

export HF_HUB_DOWNLOAD_TIMEOUT="${HF_HUB_DOWNLOAD_TIMEOUT:-120}"
export HF_HUB_ETAG_TIMEOUT="${HF_HUB_ETAG_TIMEOUT:-30}"
export HF_XET_HIGH_PERFORMANCE="${HF_XET_HIGH_PERFORMANCE:-1}"

rm -rf "${snapshot}"
mkdir -p "${snapshot}"
rm -f "${marker}"

python3 - <<'PY'
from importlib.metadata import PackageNotFoundError, version

for package in ("huggingface-hub", "hf-xet"):
    try:
        print(f"HF_DOWNLOAD_RUNTIME {package}={version(package)}")
    except PackageNotFoundError:
        print(f"HF_DOWNLOAD_RUNTIME {package}=missing")
PY

echo "Downloading FLUX.2 Klein Diffusers snapshot: ${repository}@${revision}"
echo "Download watchdog: stall=${stall_timeout}s hard=${hard_timeout}s poll=${poll_seconds}s min=${min_mibps}MiB/s grace=${throughput_grace}s window=${throughput_window}s"
HF_HUB_OFFLINE=0 python -m ai_video_factory.workers.download_watchdog \
    --progress-root "${snapshot}" \
    --stall-timeout-seconds "${stall_timeout}" \
    --hard-timeout-seconds "${hard_timeout}" \
    --poll-seconds "${poll_seconds}" \
    --label flux2-klein \
    --min-throughput-mibps "${min_mibps}" \
    --throughput-grace-seconds "${throughput_grace}" \
    --throughput-window-seconds "${throughput_window}" \
    --reallocate-on-slow \
    -- \
    hf download \
        "${repository}" \
        --revision "${revision}" \
        --local-dir "${snapshot}" \
        --include "model_index.json" \
        --include "scheduler/**" \
        --include "text_encoder/**" \
        --include "tokenizer/**" \
        --include "transformer/**" \
        --include "vae/**"

for required_path in \
    model_index.json \
    scheduler/scheduler_config.json \
    text_encoder/config.json \
    tokenizer/tokenizer_config.json \
    transformer/config.json \
    transformer/diffusion_pytorch_model.safetensors \
    vae/config.json \
    vae/diffusion_pytorch_model.safetensors; do
    if [[ ! -e "${snapshot}/${required_path}" ]]; then
        echo "FLUX.2 Klein Diffusers snapshot is incomplete; missing: ${required_path}" >&2
        exit 1
    fi
done

if [[ -e "${snapshot}/flux-2-klein-4b.safetensors" ]]; then
    echo "Unexpected monolithic FLUX.2 weight was downloaded alongside Diffusers components" >&2
    exit 1
fi

size_bytes="$(du -sb "${snapshot}" | cut -f1)"
echo "FLUX.2 Klein Diffusers snapshot complete: bytes=${size_bytes}"
printf '%s\n' "${expected_marker}" > "${marker}"
echo "FLUX.2 Klein model bootstrap complete"
