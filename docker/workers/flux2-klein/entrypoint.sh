#!/usr/bin/env bash
set -Eeuo pipefail

start_app() {
  uvicorn ai_video_factory.workers.flux2_klein.runtime:app \
    --host 0.0.0.0 \
    --port 8080 \
    --no-access-log
}

wait_for_endpoint() {
  local path="$1"
  local timeout_seconds="$2"
  local started
  started="$(date +%s)"
  while true; do
    if ! kill -0 "${app_pid}" 2>/dev/null; then
      return 1
    fi
    if python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/${path}', timeout=3)" \
      >/dev/null 2>&1; then
      return 0
    fi
    if (( $(date +%s) - started >= timeout_seconds )); then
      return 1
    fi
    sleep 5
  done
}

app_pid=""
queue_pid=""

terminate() {
  [[ -n "${app_pid}" ]] && kill -TERM "${app_pid}" 2>/dev/null || true
  [[ -n "${queue_pid}" ]] && kill -TERM "${queue_pid}" 2>/dev/null || true
}
trap terminate TERM INT EXIT

start_app &
app_pid=$!
if ! wait_for_endpoint health 120; then
  echo "FLUX.2 Klein worker HTTP health endpoint did not start" >&2
  exit 1
fi

echo "FLUX.2 Klein health endpoint is up; bootstrapping pinned model weights"
download-models

if ! wait_for_endpoint ready "${FLUX2_KLEIN_READY_TIMEOUT_SECONDS:-3600}"; then
  echo "FLUX.2 Klein worker did not become ready after model bootstrap" >&2
  exit 1
fi

if [[ "${SALAD_QUEUE_ENABLED:-false}" != "true" ]]; then
  echo "FLUX.2 Klein worker ready; queue transport disabled"
  set +e
  wait "${app_pid}"
  status=$?
  set -e
  exit "${status}"
fi

echo "FLUX.2 Klein worker ready; starting Salad queue transport"
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
