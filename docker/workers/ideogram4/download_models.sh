#!/usr/bin/env bash
set -Eeuo pipefail

: "${HF_TOKEN:?HF_TOKEN is required for the gated Ideogram 4 model}"

repo="${IDEOGRAM_MODEL_REPOSITORY:-ideogram-ai/ideogram-4-nf4}"
revision="${IDEOGRAM_MODEL_REVISION:-main}"
model_root="${IDEOGRAM_MODEL_ROOT:-/workspace/models/ideogram4}"
snapshot_dir="${IDEOGRAM_MODEL_LOCAL_SNAPSHOT:-${model_root}/snapshot}"

mkdir -p "${model_root}" "${snapshot_dir}"
rm -f "${model_root}/.ready"

echo "Downloading Ideogram 4 weights: ${repo}@${revision}"
HF_HUB_OFFLINE=0 hf download \
    "${repo}" \
    --revision "${revision}" \
    --local-dir "${snapshot_dir}"

for required_file in \
    transformer/diffusion_pytorch_model.safetensors \
    unconditional_transformer/diffusion_pytorch_model.safetensors; do
    if [[ ! -f "${snapshot_dir}/${required_file}" ]]; then
        echo "Ideogram 4 local snapshot is missing required file: ${required_file}" >&2
        exit 1
    fi
done

printf '%s@%s\n' "${repo}" "${revision}" > "${model_root}/.ready"

echo "Ideogram 4 model bootstrap complete: ${repo}@${revision}"
