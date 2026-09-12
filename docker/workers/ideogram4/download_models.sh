#!/usr/bin/env bash
set -Eeuo pipefail

: "${HF_TOKEN:?HF_TOKEN is required for the gated Ideogram 4 model}"

repo="${IDEOGRAM_MODEL_REPOSITORY:-ideogram-ai/ideogram-4-nf4}"
revision="${IDEOGRAM_MODEL_REVISION:-main}"
model_root="${IDEOGRAM_MODEL_ROOT:-/workspace/models/ideogram4}"
hf_home="${HF_HOME:-/workspace/.cache/huggingface}"
snapshot_link="${model_root}/snapshot"

mkdir -p "${model_root}" "${hf_home}/hub"

echo "Downloading Ideogram 4 weights: ${repo}@${revision}"
snapshot_path="$(HF_HUB_OFFLINE=0 hf download "${repo}" --revision "${revision}")"
snapshot_sha="$(basename "${snapshot_path}")"
cache_slug="models--${repo//\//--}"
refs_dir="${hf_home}/hub/${cache_slug}/refs"
mkdir -p "${refs_dir}"
printf '%s\n' "${snapshot_sha}" > "${refs_dir}/main"

rm -rf "${snapshot_link}"
ln -s "${snapshot_path}" "${snapshot_link}"

for required_file in \
    transformer/diffusion_pytorch_model.safetensors \
    unconditional_transformer/diffusion_pytorch_model.safetensors; do
    if [[ ! -f "${snapshot_link}/${required_file}" ]]; then
        echo "Ideogram 4 local snapshot is missing required file: ${required_file}" >&2
        exit 1
    fi
done

printf '%s@%s\n' "${repo}" "${revision}" > "${model_root}/.ready"

echo "Ideogram 4 model bootstrap complete: ${snapshot_sha}"
