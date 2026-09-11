#!/usr/bin/env bash
set -Eeuo pipefail

model_root="${WHISPER_MODEL_ROOT:-/workspace/models/whisper-large-v3-turbo}"
repository="${WHISPER_MODEL_REPOSITORY:-openai/whisper-large-v3-turbo}"
revision="${WHISPER_MODEL_REVISION:-main}"

mkdir -p "${model_root}"

if [[ -s "${model_root}/config.json" ]] && \
   find "${model_root}" -type f -name '*.safetensors' -size +0c | grep -q .; then
  echo "Whisper model already present at ${model_root}"
  exit 0
fi

echo "Downloading ${repository}@${revision} to ${model_root}"
hf download "${repository}" \
  --revision "${revision}" \
  --local-dir "${model_root}"

if [[ ! -s "${model_root}/config.json" ]]; then
  echo "Whisper config.json was not downloaded" >&2
  exit 1
fi
if ! find "${model_root}" -type f -name '*.safetensors' -size +0c | grep -q .; then
  echo "Whisper safetensors weights were not downloaded" >&2
  exit 1
fi

echo "Whisper model bootstrap complete"
