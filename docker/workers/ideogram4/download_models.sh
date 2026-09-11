#!/usr/bin/env bash
set -Eeuo pipefail

: "${HF_TOKEN:?HF_TOKEN is required for the gated Ideogram 4 model}"

repo="${IDEOGRAM_MODEL_REPOSITORY:-ideogram-ai/ideogram-4-nf4}"
revision="${IDEOGRAM_MODEL_REVISION:-main}"
model_root="${IDEOGRAM_MODEL_ROOT:-/workspace/models/ideogram4}"
hf_home="${HF_HOME:-/workspace/.cache/huggingface}"

mkdir -p "${model_root}" "${hf_home}/hub"

echo "Downloading Ideogram 4 weights: ${repo}@${revision}"
snapshot_path="$(HF_HUB_OFFLINE=0 hf download "${repo}" --revision "${revision}")"
snapshot_sha="$(basename "${snapshot_path}")"
cache_slug="models--${repo//\//--}"
refs_dir="${hf_home}/hub/${cache_slug}/refs"
mkdir -p "${refs_dir}"
printf '%s\n' "${snapshot_sha}" > "${refs_dir}/main"
printf '%s@%s\n' "${repo}" "${revision}" > "${model_root}/.ready"

echo "Ideogram 4 model bootstrap complete: ${snapshot_sha}"
