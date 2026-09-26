#!/usr/bin/env bash
set -Eeuo pipefail

# Clear a stale marker before uvicorn starts its background prepare thread.
# The downloader publishes a fresh marker only after full manifest validation.
BOOTSTRAP_COMPLETE_FILE="${LTX_MODEL_BOOTSTRAP_COMPLETE_FILE:-/tmp/ai-video-factory/ltx25-model-bootstrap.complete}"
rm -f -- "${BOOTSTRAP_COMPLETE_FILE}"

exec /usr/local/bin/common-worker-entrypoint \
  ai_video_factory.workers.ltx25.runtime:app \
  "LTX-2.5" \
  3600 \
  5 \
  download-models
