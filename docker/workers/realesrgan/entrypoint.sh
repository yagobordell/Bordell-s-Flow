#!/usr/bin/env bash
exec /usr/local/bin/common-worker-entrypoint \
  ai_video_factory.workers.realesrgan.runtime:app \
  "Real-ESRGAN" \
  240 \
  2
