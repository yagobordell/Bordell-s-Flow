#!/usr/bin/env bash
set -Eeuo pipefail

MODEL_REPOSITORY="${LTX_MODEL_REPOSITORY:-Lightricks/LTX-2.5}"
MODEL_ROOT="${LTX_MODEL_ROOT:-/workspace/models/ltx-2.5}"

MODEL_FILES=(
  diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
  text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
  vae/ltx-2.5-video-vae-bf16.safetensors
  vae/ltx-2.5-audio-vae-bf16.safetensors
  latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
)

mkdir -p "${MODEL_ROOT}"

echo "phase8 model bootstrap repository=${MODEL_REPOSITORY} root=${MODEL_ROOT}"
for model_file in "${MODEL_FILES[@]}"; do
  destination="${MODEL_ROOT}/${model_file}"
  if [[ -s "${destination}" ]]; then
    echo "MODEL_PRESENT ${model_file}"
    continue
  fi

  echo "MODEL_DOWNLOAD_START ${model_file}"
  hf download \
    "${MODEL_REPOSITORY}" \
    "${model_file}" \
    --local-dir "${MODEL_ROOT}"
  test -s "${destination}"
  echo "MODEL_DOWNLOAD_DONE ${model_file}"
done

python - <<'PY'
from pathlib import Path
import os

root = Path(os.environ.get("LTX_MODEL_ROOT", "/workspace/models/ltx-2.5"))
files = [
    "diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors",
    "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
    "vae/ltx-2.5-video-vae-bf16.safetensors",
    "vae/ltx-2.5-audio-vae-bf16.safetensors",
    "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
]
for relative in files:
    path = root / relative
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"missing model after bootstrap: {path}")
    print(f"MODEL_READY {relative} bytes={path.stat().st_size}")
PY
