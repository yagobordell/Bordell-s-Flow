#!/usr/bin/env bash
set -Eeuo pipefail

min_mbps="${SALAD_NETWORK_MIN_DOWNLOAD_MBPS:-0}"
test_bytes="${SALAD_NETWORK_TEST_BYTES:-25000000}"
attempts="${SALAD_NETWORK_TEST_ATTEMPTS:-3}"
delay_seconds="${SALAD_NETWORK_TEST_DELAY_SECONDS:-5}"
speed_test_url="${SALAD_NETWORK_TEST_URL:-https://speed.cloudflare.com/__down?bytes=${test_bytes}}"

if awk -v min="${min_mbps}" 'BEGIN {exit !(min <= 0)}'; then
  echo "SALAD_NETWORK_PREFLIGHT disabled=true"
  exit 0
fi

echo "SALAD_NETWORK_PREFLIGHT_START minimum_mbps=${min_mbps} bytes=${test_bytes} attempts=${attempts}"

best_mbps="0"
for attempt in $(seq 1 "${attempts}"); do
  if speed_bps=$(curl --fail --location --output /dev/null --silent --show-error \
      --connect-timeout 5 \
      --max-time 30 \
      --write-out "%{speed_download}" \
      "${speed_test_url}"); then
    current_mbps=$(awk -v s="${speed_bps}" 'BEGIN {printf "%.2f", s * 8 / 1000000}')
    best_mbps=$(awk -v best="${best_mbps}" -v current="${current_mbps}" \
      'BEGIN {printf "%.2f", (current > best ? current : best)}')
    echo "SALAD_NETWORK_PREFLIGHT_SAMPLE attempt=${attempt} download_mbps=${current_mbps}"

    if awk -v current="${current_mbps}" -v min="${min_mbps}" \
      'BEGIN {exit !(current >= min)}'; then
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
