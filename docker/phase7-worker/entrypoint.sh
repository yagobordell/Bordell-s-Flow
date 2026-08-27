#!/usr/bin/env bash
set -Eeuo pipefail

start_app() {
  uvicorn ai_video_factory.gpu.runtime:app \
    --host 0.0.0.0 \
    --port 8080 \
    --no-access-log
}

if [[ "${SALAD_QUEUE_ENABLED:-false}" != "true" ]]; then
  exec uvicorn ai_video_factory.gpu.runtime:app \
    --host 0.0.0.0 --port 8080 --no-access-log
fi

app_pid=""
queue_pid=""

terminate() {
  [[ -n "${app_pid}" ]] && kill -TERM "${app_pid}" 2>/dev/null || true
  [[ -n "${queue_pid}" ]] && kill -TERM "${queue_pid}" 2>/dev/null || true
}
trap terminate TERM INT EXIT

start_app &
app_pid=$!
/usr/local/bin/salad-http-job-queue-worker &
queue_pid=$!

set +e
wait -n "${app_pid}" "${queue_pid}"
status=$?
set -e
terminate
wait "${app_pid}" 2>/dev/null || true
wait "${queue_pid}" 2>/dev/null || true
exit "${status}"
