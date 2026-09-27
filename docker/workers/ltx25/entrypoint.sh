#!/usr/bin/env bash
set -Eeuo pipefail

# Each worker process needs its own validated bootstrap, even on restart.
export LTX_BOOTSTRAP_SESSION="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
rm -f "${LTX_RUNTIME_BOOTSTRAP_MARKER:-/tmp/ai-video-factory/ltx25-baked-verified}"

exec /usr/local/bin/common-worker-entrypoint \
  ai_video_factory.workers.ltx25.runtime:app \
  "LTX-2.5" \
  3600 \
  5 \
  download-models
