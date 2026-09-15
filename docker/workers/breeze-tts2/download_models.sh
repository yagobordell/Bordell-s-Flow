#!/usr/bin/env bash
set -Eeuo pipefail

model_root="${BREEZE_MODEL_ROOT:-/workspace/models/breeze-tts-2}"
repository="${BREEZE_MODEL_REPOSITORY:-BreezeBlue/Breeze-TTS-2}"
revision="${BREEZE_MODEL_REVISION:-main}"
staging_root="${model_root}.staging"
marker="${model_root}/.ready"
expected_marker="${repository}@${revision}"

if [[ -s "${marker}" ]] && \
   [[ "$(tr -d '\r\n' < "${marker}")" == "${expected_marker}" ]] && \
   [[ -s "${model_root}/config.json" ]] && \
   [[ -s "${model_root}/tokenizer.json" ]] && \
   [[ -s "${model_root}/model.safetensors.index.json" ]] && \
   [[ -s "${model_root}/audio_tokenizer/config.json" ]] && \
   [[ -s "${model_root}/audio_tokenizer/model.safetensors" ]] && \
   find "${model_root}" -maxdepth 1 -type f -name 'model-*.safetensors' -size +0c | grep -q .; then
  echo "Breeze TTS 2 model already present: ${model_root}"
  exit 0
fi

rm -rf "${staging_root}"
mkdir -p "${staging_root}"
args=(download "${repository}" --revision "${revision}" --local-dir "${staging_root}")
if [[ -n "${HF_TOKEN:-}" ]]; then
  args+=(--token "${HF_TOKEN}")
fi

echo "Downloading ${repository}@${revision} to staging directory ${staging_root}"
hf "${args[@]}"

required_files=(
  "config.json"
  "tokenizer.json"
  "model.safetensors.index.json"
  "audio_tokenizer/config.json"
  "audio_tokenizer/model.safetensors"
)
for relative_path in "${required_files[@]}"; do
  if [[ ! -s "${staging_root}/${relative_path}" ]]; then
    echo "Breeze TTS 2 bootstrap missing ${relative_path} in ${staging_root}" >&2
    exit 1
  fi
done
if ! find "${staging_root}" -maxdepth 1 -type f -name 'model-*.safetensors' -size +0c | grep -q .; then
  echo "Breeze TTS 2 bootstrap completed without model shard weights" >&2
  exit 1
fi

printf '%s\n' "${expected_marker}" > "${staging_root}/.ready"
rm -rf "${model_root}"
mv "${staging_root}" "${model_root}"

echo "Breeze TTS 2 model bootstrap complete: ${expected_marker}"
