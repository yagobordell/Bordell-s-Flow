#!/usr/bin/env bash
set -Eeuo pipefail

start_app() {
  uvicorn ai_video_factory.gpu.runtime:app \
    --host 0.0.0.0 \
    --port 8080 \
    --no-access-log
}

wait_for_app() {
  for _ in $(seq 1 60); do
    if ! kill -0 "${app_pid}" 2>/dev/null; then
      return 1
    fi
    if python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=1)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
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
if ! wait_for_app; then
  echo "Phase 7 API did not become healthy" >&2
  exit 1
fi
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
