#!/usr/bin/env bash
set -Eeuo pipefail

: "${HF_TOKEN:?HF_TOKEN is required for the gated FLUX.1-schnell model}"

repo="${FLUX_MODEL_REPOSITORY:-black-forest-labs/FLUX.1-schnell}"
revision="${FLUX_MODEL_REVISION:-main}"
model_root="${FLUX_MODEL_ROOT:-/workspace/models/flux-schnell}"
snapshot_dir="${FLUX_MODEL_LOCAL_SNAPSHOT:-${model_root}/snapshot}"
stall_timeout="${FLUX_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600}"
hard_timeout="${FLUX_DOWNLOAD_HARD_TIMEOUT_SECONDS:-2400}"
poll_seconds="${FLUX_DOWNLOAD_POLL_SECONDS:-15}"
min_mibps="${FLUX_DOWNLOAD_MIN_MIBPS:-8}"
throughput_grace="${FLUX_DOWNLOAD_THROUGHPUT_GRACE_SECONDS:-180}"
throughput_window="${FLUX_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS:-120}"

export HF_HUB_DOWNLOAD_TIMEOUT="${HF_HUB_DOWNLOAD_TIMEOUT:-120}"
export HF_HUB_ETAG_TIMEOUT="${HF_HUB_ETAG_TIMEOUT:-30}"

mkdir -p "${model_root}" "${snapshot_dir}"
rm -f "${model_root}/.ready"

echo "Downloading FLUX.1-schnell weights: ${repo}@${revision}"
HF_HUB_OFFLINE=0 python -m ai_video_factory.workers.download_watchdog \
    --progress-root "${snapshot_dir}" \
    --stall-timeout-seconds "${install_timeout}" \
    --hard-timeout-seconds "${hard_timeout}" \
    --poll-seconds "${poll_seconds}" \
    --label flux_schnell \
    --min-throughput-mibps "${min_mibps}" \
    --throughput-grace-seconds "${throughput_grace}" \
    --throughput-window-seconds "${throughput_window}" \
    --reallocate-on-slow \
    -- \
    hf download \
        "${repo}" \
        --revision "${revision}" \
        --local-dir "${snapshot_dir}"

for required_file in \
    model_index.json \
    transformer/config.json \
    text_encoder_2/config.json \
    vae/config.json; do
    if [[ ! -f "${snapshot_dir}/${required_file}" ]]; then
        echo "FLUX local snapshot is missing required file: ${required_file}" >&2
        exit 1
    fi
done

printf '%s@%s\n' "${repo}" "${revision}" > "${model_root}/.ready"
echo "FLUX.1-schnell model bootstrap complete: ${repo}@${revision}"
