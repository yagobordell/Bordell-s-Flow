#!/usr/bin/env bash
exec /usr/local/bin/common-worker-entrypoint \
  ai_video_factory.workers.whisper.runtime:app \
  "Whisper" \
  1800 \
  5 \
  download-models
