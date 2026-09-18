#!/usr/bin/env bash
set -Eeuo pipefail

model_root="${FLUX2_KLEIN_MODEL_ROOT:-/workspace/models/flux2-klein-4b}"
repository="${FLUX2_KLEIN_MODEL_REPOSITORY:-black-forest-labs/FLUX.2-klein-4B}"
revision="${FLUX2_KLEIN_MODEL_REVISION:-e7b7dc27f91deacad38e78976d1f2b499d76a294}"
snapshot="${model_root}/snapshot"
marker="${model_root}/.ready"
expected="${repository}@${revision}"

if [[ -f "${marker}" ]] && [[ "$(cat "${marker}")" == "${expected}" ]] && [[ -d "${snapshot}" ]]; then
  echo "FLUX.2 Klein model snapshot already prepared"
  exit 0
fi

rm -rf "${snapshot}"
mkdir -p "${snapshot}"

watchdog=(
  python -m ai_video_factory.workers.download_watchdog
  --progress-root "${snapshot}"
  --stall-timeout-seconds "${FLUX2_KLEIN_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600}"
  --hard-timeout-seconds "${FLUX2_KLEIN_DOWNLOAD_HARD_TIMEOUT_SECONDS:-2400}"
  --poll-seconds "${FLUX2_KLEIN_DOWNLOAD_POLL_SECONDS:-15}"
  --label "flux2-klein-4b"
  --min-throughput-mibps "${FLUX2_KLEIN_DOWNLOAD_MIN_MIBPS:-6}"
  --throughput-grace-seconds "${FLUX2_KLEIN_DOWNLOAD_THROUGHPUT_GRACE_SECONDS:-180}"
  --throughput-window-seconds "${FLUX2_KLEIN_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS:-120}"
)
if [[ "${FLUX2_KLEIN_DOWNLOAD_REALLOCATE_ON_SLOW:-true}" == "true" ]]; then
  watchdog+=(--reallocate-on-slow)
fi

echo "FLUX.2 Klein bootstrap: downloading pinned Diffusers components with Xet watchdog"
HF_HUB_OFFLINE=0 "${watchdog[@]}" --   python -m ai_video_factory.workers.flux2_klein.bootstrap

printf '%s\n' "${expected}" > "${marker}"
echo "FLUX.2 Klein model bootstrap complete: ${expected}"
