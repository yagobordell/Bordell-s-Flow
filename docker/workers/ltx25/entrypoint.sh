#!/usr/bin/env bash
set -Eeuo pipefail

bootstrap_marker="${LTX_RUNTIME_BOOTSTRAP_MARKER:-}"
if [[ -n "${bootstrap_marker}" ]]; then
  rm -f "${bootstrap_marker}"
fi

exec /usr/local/bin/common-worker-entrypoint \
  ai_video_factory.workers.ltx25.runtime:app \
  "LTX-2.5" \
  3600 \
  5 \
  download-models \
  network-preflight
