#!/usr/bin/env bash
set -Eeuo pipefail
uvicorn ai_video_factory.workers.qwen_image_21.runtime:app --host 0.0.0.0 --port 8080 --no-access-log &
app_pid=$!
queue_pid=""
terminate() { kill -TERM "${app_pid}" 2>/dev/null || true; [[ -n "${queue_pid}" ]] && kill -TERM "${queue_pid}" 2>/dev/null || true; }
trap terminate TERM INT EXIT
for _ in $(seq 1 60); do
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" >/dev/null 2>&1; then break; fi
  kill -0 "${app_pid}" 2>/dev/null || exit 1
  sleep 2
done
echo "Qwen-Image-2.1 worker health endpoint is up; bootstrapping model weights"
download-models
deadline=$((SECONDS + ${QWEN_IMAGE_21_READY_TIMEOUT_SECONDS:-1800}))
while (( SECONDS < deadline )); do
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3)" >/dev/null 2>&1; then break; fi
  kill -0 "${app_pid}" 2>/dev/null || exit 1
  sleep 5
done
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3)" >/dev/null 2>&1 || { echo "Qwen-Image-2.1 worker readiness timeout" >&2; exit 1; }
if [[ "${SALAD_QUEUE_ENABLED:-false}" != "true" ]]; then wait "${app_pid}"; exit $?; fi
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
