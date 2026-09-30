#!/usr/bin/env bash
set -euo pipefail

runtime_pid=""
socat_pid=""

terminate_children() {
    if [[ -n "$socat_pid" ]]; then
        kill -TERM "$socat_pid" 2>/dev/null || true
    fi
    if [[ -n "$runtime_pid" ]]; then
        kill -TERM "$runtime_pid" 2>/dev/null || true
    fi
}

trap terminate_children TERM INT

python -m image_upscaler &
runtime_pid=$!

socat TCP6-LISTEN:8080,reuseaddr,fork TCP4:127.0.0.1:8081 &
socat_pid=$!

set +e
wait -n "$runtime_pid" "$socat_pid"
status=$?
set -e

terminate_children
wait "$runtime_pid" 2>/dev/null || true
wait "$socat_pid" 2>/dev/null || true
exit "$status"
