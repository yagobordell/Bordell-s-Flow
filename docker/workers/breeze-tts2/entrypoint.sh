#!/usr/bin/env bash
set -Eeuo pipefail

start_app() {
  uvicorn ai_video_factory.workers.breeze_tts2.runtime:app \
    --host 0.0.0.0 \
    --port 8080 \
    --no-access-log
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
  for _ in $(seq 1 720); do
    if ! kill -0 "${app_pid}" 2>/dev/null; then
      return 1
    fi
    if python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
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
  echo "Breeze TTS 2 worker HTTP health endpoint did not start" >&2
  exit 1
fi

echo "Breeze worker health endpoint is up; bootstrapping model weights"
download-models

if ! wait_for_ready; then
  echo "Breeze TTS 2 worker did not become ready after model bootstrap/warmup" >&2
  exit 1
fi

if [[ "${SALAD_QUEUE_ENABLED:-false}" != "true" ]]; then
  echo "Breeze TTS 2 worker ready; queue transport disabled"
  set +e
  wait "${app_pid}"
  status=$?
  set -e
  exit "${status}"
fi

echo "Breeze TTS 2 worker ready; starting Salad queue transport"
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
