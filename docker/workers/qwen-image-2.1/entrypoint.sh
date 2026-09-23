#!/usr/bin/env bash
exec /usr/local/bin/common-worker-entrypoint \
  ai_video_factory.workers.qwen_image_21.runtime:app \
  "Qwen-Image-2.1" \
  "${QWEN_IMAGE_21_READY_TIMEOUT_SECONDS:-1800}" \
  5 \
  download-models
