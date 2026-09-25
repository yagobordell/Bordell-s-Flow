#!/usr/bin/env bash
set -Eeuo pipefail

MANIFEST_PATH="${LTX_MODEL_MANIFEST:-/usr/local/share/ltx25-model-manifest.json}"
MODEL_ROOT="${LTX_MODEL_ROOT:-/workspace/models/ltx-2.5}"
MODEL_REPOSITORY="${LTX_MODEL_REPOSITORY:-Lightricks/LTX-2.5}"
MODEL_REVISION="${LTX_MODEL_REVISION:-6c7e5e573ac1667efc83407806fe9b0b93730e60}"
INCLUDE_DEV_ASSETS="${LTX_INCLUDE_A2V_DEV_ASSETS:-false}"
INSTALLED_MANIFEST="${MODEL_ROOT}/.bordell-installed-model-manifest.json"

POLL_SECONDS="${LTX_MODEL_DOWNLOAD_PROGRESS_INTERVAL_SECONDS:-30}"
STALL_TIMEOUT_SECONDS="${LTX_MODEL_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600}"
HARD_TIMEOUT_SECONDS="${LTX_MODEL_DOWNLOAD_HARD_TIMEOUT_SECONDS:-21600}"
MIN_PROGRESS_RESET_BYTES="${LTX_MODEL_DOWNLOAD_MIN_PROGRESS_RESET_BYTES:-67108864}"
MIN_THROUGHPUT_MIBPS="${LTX_MODEL_DOWNLOAD_MIN_THROUGHPUT_MIBPS:-8}"
THROUGHPUT_GRACE_SECONDS="${LTX_MODEL_DOWNLOAD_THROUGHPUT_GRACE_SECONDS:-300}"
THROUGHPUT_WINDOW_SECONDS="${LTX_MODEL_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS:-180}"

[[ -r "${MANIFEST_PATH}" ]] || {
  echo "missing LTX model manifest: ${MANIFEST_PATH}" >&2
  exit 1
}

mapfile -t MANIFEST_VALUES < <(
  python3 - "${MANIFEST_PATH}" "${INCLUDE_DEV_ASSETS}" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
include_dev = sys.argv[2].strip().lower() in {"1", "true", "yes", "on"}
print(manifest["repository"])
print(manifest["revision"])
for item in manifest["shared_files"]:
    print("FILE\t" + item)
if include_dev:
    for item in manifest["dev_files"]:
        print("FILE\t" + item)
PY
)

EXPECTED_REPOSITORY="${MANIFEST_VALUES[0]}"
EXPECTED_REVISION="${MANIFEST_VALUES[1]}"
MODEL_FILES=()
for value in "${MANIFEST_VALUES[@]:2}"; do
  [[ "${value}" == FILE$'\t'* ]] || continue
  MODEL_FILES+=("${value#FILE$'\t'}")
done

if [[ "${MODEL_REPOSITORY}" != "${EXPECTED_REPOSITORY}" ]]; then
  echo "LTX model repository does not match pinned manifest: ${MODEL_REPOSITORY} != ${EXPECTED_REPOSITORY}" >&2
  exit 1
fi
if [[ "${MODEL_REVISION}" != "${EXPECTED_REVISION}" ]]; then
  echo "LTX model revision does not match pinned manifest: ${MODEL_REVISION} != ${EXPECTED_REVISION}" >&2
  exit 1
fi

mkdir -p "${MODEL_ROOT}"

manifest_is_valid=false
if python3 - "${INSTALLED_MANIFEST}" "${MODEL_REPOSITORY}" "${MODEL_REVISION}" "${MODEL_ROOT}" "${MODEL_FILES[@]}" <<'PY'
import json
import sys
from pathlib import Path

installed_path = Path(sys.argv[1])
repository = sys.argv[2]
revision = sys.argv[3]
root = Path(sys.argv[4])
expected_files = sys.argv[5:]

if not installed_path.is_file():
    raise SystemExit(1)
try:
    installed = json.loads(installed_path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)

if installed.get("repository") != repository or installed.get("revision") != revision:
    raise SystemExit(1)

records = installed.get("files")
if not isinstance(records, dict) or sorted(records) != sorted(expected_files):
    raise SystemExit(1)

for relative in expected_files:
    record = records.get(relative)
    if not isinstance(record, dict):
        raise SystemExit(1)
    path = root / relative
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(1)
    if int(record.get("size", -1)) != path.stat().st_size:
        raise SystemExit(1)

raise SystemExit(0)
PY
then
  manifest_is_valid=true
fi

if [[ "${manifest_is_valid}" == "false" ]]; then
  /usr/local/bin/network-preflight
fi

python3 - <<'PY'
from importlib.metadata import version
import os

print(
    "HF_DOWNLOAD_RUNTIME "
    f"huggingface_hub={version('huggingface-hub')} "
    f"hf_xet={version('hf-xet')} "
    f"download_timeout={os.environ.get('HF_HUB_DOWNLOAD_TIMEOUT', '-')} "
    f"etag_timeout={os.environ.get('HF_HUB_ETAG_TIMEOUT', '-')} "
    f"xet_disabled={os.environ.get('HF_HUB_DISABLE_XET', 'false')}"
)
PY

echo "ltx25 model bootstrap repository=${MODEL_REPOSITORY} revision=${MODEL_REVISION} root=${MODEL_ROOT} include_dev=${INCLUDE_DEV_ASSETS}"

if [[ "${manifest_is_valid}" == "true" ]]; then
  echo "MODEL_MANIFEST_VALID path=${INSTALLED_MANIFEST}"
else
  for model_file in "${MODEL_FILES[@]}"; do
    destination="${MODEL_ROOT}/${model_file}"
    echo "MODEL_VERIFY_START ${model_file}"
    python -m ai_video_factory.workers.download_watchdog \
      --progress-root "${MODEL_ROOT}" \
      --stall-timeout-seconds "${STALL_TIMEOUT_SECONDS}" \
      --hard-timeout-seconds "${HARD_TIMEOUT_SECONDS}" \
      --poll-seconds "${POLL_SECONDS}" \
      --min-progress-reset-bytes "${MIN_PROGRESS_RESET_BYTES}" \
      --min-throughput-mibps "${MIN_THROUGHPUT_MIBPS}" \
      --throughput-grace-seconds "${THROUGHPUT_GRACE_SECONDS}" \
      --throughput-window-seconds "${THROUGHPUT_WINDOW_SECONDS}" \
      --label "ltx25:${model_file}" \
      --reallocate-on-slow \
      -- \
      hf download "${MODEL_REPOSITORY}" "${model_file}" \
        --revision "${MODEL_REVISION}" \
        --local-dir "${MODEL_ROOT}"
    [[ -s "${destination}" ]] || {
      echo "missing model after pinned bootstrap: ${destination}" >&2
      exit 1
    }
    echo "MODEL_VERIFY_DONE ${model_file} bytes=$(stat -c %s "${destination}")"
  done

  python3 - "${INSTALLED_MANIFEST}" "${MODEL_REPOSITORY}" "${MODEL_REVISION}" "${MODEL_ROOT}" "${MODEL_FILES[@]}" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

installed_path = Path(sys.argv[1])
repository = sys.argv[2]
revision = sys.argv[3]
root = Path(sys.argv[4])
files = sys.argv[5:]

records = {}
for relative in files:
    path = root / relative
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(16 * 1024 * 1024), b""):
            digest.update(chunk)
    records[relative] = {
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }

payload = {
    "schema_version": 1,
    "repository": repository,
    "revision": revision,
    "files": records,
}
tmp_path = installed_path.with_suffix(".tmp")
tmp_path.write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
os.replace(tmp_path, installed_path)
print(f"MODEL_MANIFEST_WRITTEN path={installed_path} files={len(records)}")
PY
fi

for model_file in "${MODEL_FILES[@]}"; do
  destination="${MODEL_ROOT}/${model_file}"
  [[ -s "${destination}" ]] || {
    echo "model missing after manifest validation: ${destination}" >&2
    exit 1
  }
  echo "MODEL_READY ${model_file} bytes=$(stat -c %s "${destination}")"
done
