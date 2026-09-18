#!/usr/bin/env bash
set -Eeuo pipefail

bootstrap_status_path="${FLUX2_KLEIN_BOOTSTRAP_STATUS_PATH:-/tmp/ai-video-factory/flux2-klein-bootstrap.json}"
bootstrap_stage_timeout="${FLUX2_KLEIN_BOOTSTRAP_STALL_TIMEOUT_SECONDS:-600}"
bootstrap_hard_timeout="${FLUX2_KLEIN_BOOTSTRAP_HARD_TIMEOUT_SECONDS:-900}"
bootstrap_poll_seconds="${FLUX2_KLEIN_BOOTSTRAP_POLL_SECONDS:-15}"

start_app() {
  uvicorn ai_video_factory.workers.flux2_klein.runtime:app \
    --host 0.0.0.0 \
    --port 8080 \
    --no-access-log
}

start_bootstrap_watchdog() {
  local args=(
    --status-path "${bootstrap_status_path}"
    --stage-timeout-seconds "${bootstrap_stage_timeout}"
    --hard-timeout-seconds "${bootstrap_hard_timeout}"
    --poll-seconds "${bootstrap_poll_seconds}"
  )
  if [[ "${FLUX2_KLEIN_BOOTSTRAP_REALLOCATE_ON_STALL:-true}" == "true" ]]; then
    args+=(--reallocate-on-stall)
  fi
  python -m ai_video_factory.workers.flux2_klein.bootstrap_watchdog "${args[@]}"
}

wait_for_health() {
  for _ in $(seq 1 60); do
    if ! kill -0 "${app_pid}" 2>/dev/null; then
      return 1
    fi
    if python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_ready() {
  for _ in $(seq 1 240); do
    if ! kill -0 "${app_pid}" 2>/dev/null; then
      return 1
    fi
    if ! kill -0 "${watchdog_pid}" 2>/dev/null; then
      wait "${watchdog_pid}" || true
      return 1
    fi
    if python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3)" \
      >/dev/null 2>&1; then
      wait "${watchdog_pid}"
      watchdog_pid=""
      return 0
    fi
    sleep 5
  done
  return 1
}

app_pid=""
queue_pid=""
watchdog_pid=""

terminate() {
  [[ -n "${watchdog_pid}" ]] && kill -TERM "${watchdog_pid}" 2>/dev/null || true
  [[ -n "${app_pid}" ]] && kill -TERM "${app_pid}" 2>/dev/null || true
  [[ -n "${queue_pid}" ]] && kill -TERM "${queue_pid}" 2>/dev/null || true
}
trap terminate TERM INT EXIT

mkdir -p "$(dirname "${bootstrap_status_path}")"
rm -f "${bootstrap_status_path}" "${bootstrap_status_path}.tmp"

start_app &
app_pid=$!
if ! wait_for_health; then
  echo "FLUX.2 Klein worker HTTP health endpoint did not start" >&2
  exit 1
fi

echo "FLUX.2 Klein worker health endpoint is up; bootstrapping model weights"
download-models

echo "FLUX.2 Klein model files downloaded; starting runtime bootstrap watchdog"
start_bootstrap_watchdog &
watchdog_pid=$!

if ! wait_for_ready; then
  echo "FLUX.2 Klein worker did not become ready within the bounded runtime bootstrap budget" >&2
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
