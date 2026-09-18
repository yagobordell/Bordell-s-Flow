#!/usr/bin/env bash
set -Eeuo pipefail

start_app() {
  uvicorn ai_video_factory.workers.flux2_klein.runtime:app     --host 0.0.0.0     --port 8080     --no-access-log
}

wait_for_health() {
  for _ in $(seq 1 60); do
    if ! kill -0 "${app_pid}" 2>/dev/null; then
      return 1
    fi
    if python -c       "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"       >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_ready() {
  local deadline
  deadline=$((SECONDS + ${FLUX2_KLEIN_READY_TIMEOUT_SECONDS:-1200}))
  while (( SECONDS < deadline )); do
    if ! kill -0 "${app_pid}" 2>/dev/null; then
      return 1
    fi
    if python -c       "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3)"       >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

request_reallocation() {
  python - <<'PY' || true
from ai_video_factory.workers.download_watchdog import request_salad_reallocation

reason = "FLUX.2 Klein runtime did not become ready within bounded bootstrap timeout"
print(
    "SALAD_REALLOCATION_REQUEST "
    f"source=flux2_klein_runtime requested={str(request_salad_reallocation(reason)).lower()} "
    f"reason={reason}",
    flush=True,
)
PY
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
if ! wait_for_health; then
  echo "FLUX.2 Klein worker HTTP health endpoint did not start" >&2
  exit 1
fi

echo "FLUX.2 Klein worker health endpoint is up; bootstrapping model weights"
download-models

if ! wait_for_ready; then
  echo "FLUX.2 Klein worker did not become ready after model bootstrap" >&2
  request_reallocation
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
