#!/usr/bin/env bash
set -Eeuo pipefail

app="${1:?worker app is required}"
worker_name="${2:?worker name is required}"
ready_timeout_seconds="${3:?ready timeout is required}"
ready_poll_seconds="${4:-5}"
bootstrap_command="${5:-}"

app_pid=""
queue_pid=""

terminate() {
  [[ -n "${app_pid}" ]] && kill -TERM "${app_pid}" 2>/dev/null || true
  [[ -n "${queue_pid}" ]] && kill -TERM "${queue_pid}" 2>/dev/null || true
}
trap terminate TERM INT EXIT

wait_for_endpoint() {
  local path="$1"
  local timeout_seconds="$2"
  local poll_seconds="$3"
  local deadline=$((SECONDS + timeout_seconds))

  while (( SECONDS < deadline )); do
    kill -0 "${app_pid}" 2>/dev/null || return 1
    if python -c       "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080${path}', timeout=3)"       >/dev/null 2>&1; then
      return 0
    fi
    sleep "${poll_seconds}"
  done
  return 1
}

uvicorn "${app}" --host 0.0.0.0 --port 8080 --no-access-log &
app_pid=$!

if ! wait_for_endpoint /health 120 2; then
  echo "${worker_name} worker HTTP health endpoint did not start" >&2
  exit 1
fi

if [[ -n "${bootstrap_command}" ]]; then
  echo "${worker_name} worker health endpoint is up; bootstrapping model weights"
  "${bootstrap_command}"
fi

if ! wait_for_endpoint /ready "${ready_timeout_seconds}" "${ready_poll_seconds}"; then
  echo "${worker_name} worker did not become ready" >&2
  exit 1
fi

if [[ "${SALAD_QUEUE_ENABLED:-false}" != "true" ]]; then
  echo "${worker_name} worker ready; queue transport disabled"
  set +e
  wait "${app_pid}"
  status=$?
  set -e
  exit "${status}"
fi

echo "${worker_name} worker ready; starting Salad queue transport"
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
