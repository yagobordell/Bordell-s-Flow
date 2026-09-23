#!/usr/bin/env bash
exec /usr/local/bin/common-worker-entrypoint \
  ai_video_factory.workers.breeze_tts2.runtime:app \
  "Breeze TTS 2" \
  3600 \
  5 \
  download-models
