#!/usr/bin/env bash
set -Eeuo pipefail

model_root="${FLUX2_KLEIN_MODEL_ROOT:-/workspace/models/flux2-klein-4b}"
repository="${FLUX2_KLEIN_MODEL_REPOSITORY:-black-forest-labs/FLUX.2-klein-4B}"
revision="${FLUX2_KLEIN_MODEL_REVISION:-e7b7dc27f91deacad38e78976d1f2b499d76a294}"
snapshot="${model_root}/snapshot"
marker="${model_root}/.ready"
download_root="${HF_HOME:-/workspace/.cache/huggingface}"

expected="${repository}@${revision}"
if [[ -f "${marker}" ]] && [[ "$(cat "${marker}")" == "${expected}" ]] && [[ -d "${snapshot}" ]]; then
  echo "FLUX.2 Klein model snapshot already prepared"
  exit 0
fi

rm -rf "${snapshot}"
mkdir -p "${snapshot}" "${download_root}"

echo "FLUX.2 Klein bootstrap: downloading pinned Diffusers runtime components once"
python -m ai_video_factory.workers.download_watchdog   --progress-root "${download_root}"   --stall-timeout-seconds "${FLUX2_KLEIN_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600}"   --hard-timeout-seconds "${FLUX2_KLEIN_DOWNLOAD_HARD_TIMEOUT_SECONDS:-2400}"   --poll-seconds "${FLUX2_KLEIN_DOWNLOAD_POLL_SECONDS:-15}"   --label "flux2-klein-4b"   --min-throughput-mibps "${FLUX2_KLEIN_DOWNLOAD_MIN_MIBPS:-6}"   --throughput-grace-seconds "${FLUX2_KLEIN_DOWNLOAD_THROUGHPUT_GRACE_SECONDS:-180}"   --throughput-window-seconds "${FLUX2_KLEIN_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS:-120}"   --reallocate-on-slow   -- python - <<'PY'
import os
from pathlib import Path

from huggingface_hub import snapshot_download

model_root = Path(os.environ.get("FLUX2_KLEIN_MODEL_ROOT", "/workspace/models/flux2-klein-4b"))
snapshot = model_root / "snapshot"
repository = os.environ.get(
    "FLUX2_KLEIN_MODEL_REPOSITORY",
    "black-forest-labs/FLUX.2-klein-4B",
)
revision = os.environ.get(
    "FLUX2_KLEIN_MODEL_REVISION",
    "e7b7dc27f91deacad38e78976d1f2b499d76a294",
)
allow_patterns = [
    "model_index.json",
    "scheduler/**",
    "text_encoder/**",
    "tokenizer/**",
    "transformer/**",
    "vae/**",
]

snapshot_download(
    repo_id=repository,
    revision=revision,
    local_dir=str(snapshot),
    token=os.environ.get("HF_TOKEN"),
    allow_patterns=allow_patterns,
)

required = [
    snapshot / "model_index.json",
    snapshot / "scheduler",
    snapshot / "text_encoder",
    snapshot / "tokenizer",
    snapshot / "transformer",
    snapshot / "vae",
]
missing = [str(path.relative_to(snapshot)) for path in required if not path.exists()]
if missing:
    raise RuntimeError(
        "FLUX.2 Klein Diffusers snapshot is incomplete; missing: " + ", ".join(missing)
    )

size_bytes = sum(path.stat().st_size for path in snapshot.rglob("*") if path.is_file())
print(f"FLUX.2 Klein Diffusers snapshot complete: bytes={size_bytes}", flush=True)
PY

printf '%s\n' "${expected}" > "${marker}"
echo "FLUX.2 Klein model bootstrap complete"
