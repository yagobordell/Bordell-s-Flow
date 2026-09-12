#!/usr/bin/env bash
set -Eeuo pipefail

model_root="${WHISPER_MODEL_ROOT:-/workspace/models/whisper-large-v3-turbo}"
repository="${WHISPER_MODEL_REPOSITORY:-openai/whisper-large-v3-turbo}"
revision="${WHISPER_MODEL_REVISION:-main}"
staging_root="${model_root}.staging"

mkdir -p "${model_root}"

if [[ -s "${model_root}/config.json" ]] && \
   find "${model_root}" -type f -name '*.safetensors' -size +0c | grep -q .; then
  echo "Whisper model already present at ${model_root}"
  exit 0
fi

rm -rf "${staging_root}"
mkdir -p "${staging_root}"

echo "Downloading ${repository}@${revision} to staging directory ${staging_root}"
hf download "${repository}" \
  --revision "${revision}" \
  --local-dir "${staging_root}"

if [[ ! -s "${staging_root}/config.json" ]]; then
  echo "Whisper config.json was not downloaded" >&2
  exit 1
fi
if ! find "${staging_root}" -type f -name '*.safetensors' -size +0c | grep -q .; then
  echo "Whisper safetensors weights were not downloaded" >&2
  exit 1
fi

rm -rf "${model_root}"
mv "${staging_root}" "${model_root}"

echo "Whisper model bootstrap complete"
