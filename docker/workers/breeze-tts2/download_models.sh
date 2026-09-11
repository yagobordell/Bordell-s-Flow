#!/usr/bin/env bash
set -Eeuo pipefail

model_root="${BREEZE_MODEL_ROOT:-/workspace/models/breeze-tts-2}"
repository="${BREEZE_MODEL_REPOSITORY:-BreezeBlue/Breeze-TTS-2}"
revision="${BREEZE_MODEL_REVISION:-main}"

if [[ -f "${model_root}/config.json" ]]; then
  echo "Breeze TTS 2 model already present: ${model_root}"
  exit 0
fi

mkdir -p "${model_root}"
args=(download "${repository}" --revision "${revision}" --local-dir "${model_root}")
if [[ -n "${HF_TOKEN:-}" ]]; then
  args+=(--token "${HF_TOKEN}")
fi

hf "${args[@]}"

if [[ ! -f "${model_root}/config.json" ]]; then
  echo "Breeze TTS 2 bootstrap completed without config.json in ${model_root}" >&2
  exit 1
fi

echo "Breeze TTS 2 model bootstrap complete: ${repository}@${revision}"
