#!/usr/bin/env bash
set -Eeuo pipefail

model_root="${FISH_SPEECH_MODEL_ROOT:-/workspace/models/fish-s2-pro}"
repository="${FISH_SPEECH_MODEL_REPOSITORY:-fishaudio/s2-pro}"
revision="${FISH_SPEECH_MODEL_REVISION:-1de9996b6be38b745688de084d87a5633f714e4e}"
runtime_commit="${FISH_SPEECH_RUNTIME_COMMIT:-214da3cd841bda85da2496b96cd3c4d7edb1337e}"
staging_root="${model_root}.staging"
marker="${model_root}/.ready"
expected_marker="${repository}@${revision}|runtime@${runtime_commit}"

if [[ -s "${marker}" ]] \
   && [[ "$(tr -d '\r\n' < "${marker}")" == "${expected_marker}" ]] \
   && [[ -s "${model_root}/snapshot/config.json" ]] \
   && [[ -s "${model_root}/snapshot/model.safetensors.index.json" ]] \
   && [[ -s "${model_root}/snapshot/model-00001-of-00002.safetensors" ]] \
   && [[ -s "${model_root}/snapshot/model-00002-of-00002.safetensors" ]] \
   && [[ -s "${model_root}/snapshot/codec.pth" ]] \
   && [[ -s "${model_root}/snapshot/tokenizer.json" ]]; then
  echo "Fish Speech model already prepared: ${expected_marker}"
  exit 0
fi

rm -rf "${staging_root}"
mkdir -p "${staging_root}/snapshot"

echo "Fish Speech bootstrap: downloading pinned official S2 Pro checkpoint"
python -m ai_video_factory.workers.download_watchdog \
  --progress-root "${staging_root}" \
  --stall-timeout-seconds "${FISH_SPEECH_DOWNLOAD_STALL_TIMEOUT_SECONDS:-600}" \
  --hard-timeout-seconds "${FISH_SPEECH_DOWNLOAD_HARD_TIMEOUT_SECONDS:-2400}" \
  --poll-seconds "${FISH_SPEECH_DOWNLOAD_POLL_SECONDS:-15}" \
  --label fish-s2-pro \
  --min-throughput-mibps "${FISH_SPEECH_DOWNLOAD_MIN_MIBPS:-6}" \
  --throughput-grace-seconds "${FISH_SPEECH_DOWNLOAD_THROUGHPUT_GRACE_SECONDS:-180}" \
  --throughput-window-seconds "${FISH_SPEECH_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS:-120}" \
  --reallocate-on-slow \
  -- env HF_HUB_OFFLINE=0 python - <<'PY'
import os
from pathlib import Path

from huggingface_hub import snapshot_download

root = Path(os.environ.get("FISH_SPEECH_MODEL_ROOT", "/workspace/models/fish-s2-pro"))
staging = Path(str(root) + ".staging") / "snapshot"
repo = os.environ.get("FISH_SPEECH_MODEL_REPOSITORY", "fishaudio/s2-pro")
revision = os.environ.get(
    "FISH_SPEECH_MODEL_REVISION",
    "1de9996b6be38b745688de084d87a5633f714e4e",
)

snapshot_download(
    repo_id=repo,
    revision=revision,
    local_dir=str(staging),
    token=os.environ.get("HF_TOKEN"),
    allow_patterns=[
        "chat_template.jinja",
        "codec.pth",
        "config.json",
        "model-*.safetensors",
        "model.safetensors.index.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    ],
)

required = [
    "config.json",
    "model.safetensors.index.json",
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
    "codec.pth",
    "tokenizer.json",
]
missing = [name for name in required if not (staging / name).is_file()]
if missing:
    raise RuntimeError("Fish Speech snapshot incomplete: " + ", ".join(missing))
size_bytes = sum(path.stat().st_size for path in staging.rglob("*") if path.is_file())
print(f"FISH_SPEECH_CHECKPOINT_READY bytes={size_bytes}", flush=True)
PY

printf '%s\n' "${expected_marker}" > "${staging_root}/.ready.tmp"
mv "${staging_root}/.ready.tmp" "${staging_root}/.ready"
rm -rf "${model_root}"
mv "${staging_root}" "${model_root}"
echo "Fish Speech model bootstrap complete: ${expected_marker}"
