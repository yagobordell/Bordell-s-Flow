#!/usr/bin/env bash
set -Eeuo pipefail

: "${HF_TOKEN:?HF_TOKEN is required for the gated Ideogram 4 model}"

repo="${IDEOGRAM_MODEL_REPOSITORY:-ideogram-ai/ideogram-4-nf4}"
revision="${IDEOGRAM_MODEL_REVISION:-main}"
model_root="${IDEOGRAM_MODEL_ROOT:-/workspace/models/ideogram4}"
snapshot_dir="${IDEOGRAM_MODEL_LOCAL_SNAPSHOT:-${model_root}/snapshot}"
stall_timeout="${IDEOGRAM_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600}"
hard_timeout="${IDEOGRAM_DOWNLOAD_HARD_TIMEOUT_SECONDS:-1800}"
poll_seconds="${IDEOGRAM_DOWNLOAD_POLL_SECONDS:-15}"

export HF_HUB_DOWNLOAD_TIMEOUT="${HF_HUB_DOWNLOAD_TIMEOUT:-120}"
export HF_HUB_ETAG_TIMEOUT="${HF_HUB_ETAG_TIMEOUT:-30}"

mkdir -p "${model_root}" "${snapshot_dir}"
rm -f "${model_root}/.ready"

echo "Downloading Ideogram 4 weights: ${repo}@${revision}"
echo "Download watchdog: stall=${stall_timeout}s hard=${hard_timeout}s poll=${poll_seconds}s"
# Keep the original HF_HUB_OFFLINE=0 hf download behavior, but execute it under a byte-progress
# watchdog so an unhealthy host is replaced instead of holding a GPU indefinitely.
HF_HUB_OFFLINE=0 python -m ai_video_factory.workers.ideogram4.download_watchdog \
    --progress-root "${snapshot_dir}" \
    --stall-timeout-seconds "${stall_timeout}" \
    --hard-timeout-seconds "${hard_timeout}" \
    --poll-seconds "${poll_seconds}" \
    -- \
    hf download \
        "${repo}" \
        --revision "${revision}" \
        --local-dir "${snapshot_dir}"

for required_file in \
    transformer/diffusion_pytorch_model.safetensors \
    unconditional_transformer/diffusion_pytorch_model.safetensors; do
    if [[ ! -f "${snapshot_dir}/${required_file}" ]]; then
        echo "Ideogram 4 local snapshot is missing required file: ${required_file}" >&2
        exit 1
    fi
done

printf '%s@%s\n' "${repo}" "${revision}" > "${model_root}/.ready"

echo "Ideogram 4 model bootstrap complete: ${repo}@${revision}"
