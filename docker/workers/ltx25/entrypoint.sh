#!/usr/bin/env bash
exec /usr/local/bin/common-worker-entrypoint \
  ai_video_factory.workers.ltx25.runtime:app \
  "LTX-2.5" \
  3600 \
  5 \
  download-models
