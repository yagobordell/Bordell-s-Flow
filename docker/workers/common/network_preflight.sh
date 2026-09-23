#!/usr/bin/env bash
set -Eeuo pipefail

min_mbps="${SALAD_NETWORK_MIN_DOWNLOAD_MBPS:-0}"
test_bytes="${SALAD_NETWORK_TEST_BYTES:-25000000}"
attempts="${SALAD_NETWORK_TEST_ATTEMPTS:-3}"
delay_seconds="${SALAD_NETWORK_TEST_DELAY_SECONDS:-5}"
configured_test_url="${SALAD_NETWORK_TEST_URL:-}"
if [[ -n "${configured_test_url}" ]]; then
  speed_test_url="${configured_test_url}"
  use_range=true
else
  speed_test_url="https://speed.cloudflare.com/__down?bytes=${test_bytes}"
  use_range=false
fi

if python3 -c 'import sys; raise SystemExit(0 if float(sys.argv[1]) <= 0 else 1)' "${min_mbps}"; then
  echo "SALAD_NETWORK_PREFLIGHT disabled=true"
  exit 0
fi

echo "SALAD_NETWORK_PREFLIGHT_START minimum_mbps=${min_mbps} bytes=${test_bytes} attempts=${attempts} target=${speed_test_url}"

best_mbps="0"
for attempt in $(seq 1 "${attempts}"); do
  curl_args=(
    --fail
    --location
    --output /dev/null
    --silent
    --show-error
    --connect-timeout 5
    --max-time 30
    --write-out "%{speed_download}"
  )
  if [[ "${use_range}" == "true" ]]; then
    curl_args+=(--range "0-$((test_bytes - 1))")
  fi

  if speed_bps=$(curl "${curl_args[@]}" "${speed_test_url}"); then
    current_mbps=$(python3 -c 'import sys; print(f"{float(sys.argv[1]) * 8 / 1_000_000:.2f}")' "${speed_bps}")
    best_mbps=$(python3 -c 'import sys; print(f"{max(float(sys.argv[1]), float(sys.argv[2])):.2f}")' "${best_mbps}" "${current_mbps}")
    echo "SALAD_NETWORK_PREFLIGHT_SAMPLE attempt=${attempt} download_mbps=${current_mbps}"

    if python3 -c 'import sys; raise SystemExit(0 if float(sys.argv[1]) >= float(sys.argv[2]) else 1)' \
      "${current_mbps}" "${min_mbps}"; then
      echo "SALAD_NETWORK_PREFLIGHT_PASS download_mbps=${current_mbps} minimum_mbps=${min_mbps}"
      exit 0
    fi
  else
    echo "SALAD_NETWORK_PREFLIGHT_SAMPLE attempt=${attempt} status=failed"
  fi

  if [[ "${attempt}" -lt "${attempts}" ]]; then
    sleep "${delay_seconds}"
  fi
done

reason="Insufficient startup download bandwidth: best ${best_mbps} Mbps, required ${min_mbps} Mbps"
echo "SALAD_NETWORK_PREFLIGHT_FAIL reason=${reason}" >&2
echo "SALAD_REALLOCATION_REQUEST reason=${reason}" >&2
curl --silent --show-error \
  --request POST \
  --url "http://169.254.169.254/v1/reallocate" \
  --header "Content-Type: application/json" \
  --header "Metadata: true" \
  --data "{\"reason\":\"Insufficient startup download bandwidth\"}" \
  >/dev/null || true
exit 1
