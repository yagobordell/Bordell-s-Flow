#!/usr/bin/env bash
set -Eeuo pipefail

start_app() {
  uvicorn ai_video_factory.workers.fish_speech.runtime:app     --host 0.0.0.0     --port 8080     --no-access-log
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
  deadline=$((SECONDS + ${FISH_SPEECH_READY_TIMEOUT_SECONDS:-1800}))
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

reason = "Fish Speech runtime did not become ready within bounded bootstrap timeout"
print(
    "SALAD_REALLOCATION_REQUEST "
    f"source=fish_speech_runtime requested={str(request_salad_reallocation(reason)).lower()} "
    f"reason={reason}",
    flush=True,
)
PY
}

app_pid=""

terminate() {
  [[ -n "${app_pid}" ]] && kill -TERM "${app_pid}" 2>/dev/null || true
}
trap terminate TERM INT EXIT

start_app &
app_pid=$!
if ! wait_for_health; then
  echo "Fish Speech worker HTTP health endpoint did not start" >&2
  exit 1
fi

echo "Fish Speech health endpoint is up; bootstrapping pinned model weights"
download-models

if ! wait_for_ready; then
  echo "Fish Speech worker did not become ready after model bootstrap/model load" >&2
  request_reallocation
  exit 1
fi

echo "Fish Speech worker ready; polling canonical Postgres jobs"
set +e
wait "${app_pid}"
status=$?
set -e
exit "${status}"
