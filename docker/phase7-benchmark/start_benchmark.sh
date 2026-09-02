#!/usr/bin/env bash
set -Eeuo pipefail

export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
export HF_HUB_ENABLE_HF_TRANSFER="${HF_HUB_ENABLE_HF_TRANSFER:-0}"

PYTHON=/opt/ltx/.venv/bin/python
FACTORY=/opt/factory
PUBLIC_ROOT=/workspace/public
RESULTS_ROOT="${PUBLIC_ROOT}/results"
MODELS_ROOT=/workspace/models/ltx-2.5
STATUS_PATH="${RESULTS_ROOT}/status.json"
LOG_PATH="${RESULTS_ROOT}/benchmark.log"
KEYFRAME="${FACTORY}/docker/phase7-benchmark/assets/shot_001.png"
MODEL_REPOSITORY=Lightricks/LTX-2.5

BENCHMARK_CASE="${BENCHMARK_CASE:-distilled-fp8-cpu}"
BENCHMARK_HARDWARE="${BENCHMARK_HARDWARE:-rtx5090}"
BENCHMARK_MEASURED_RUNS="${BENCHMARK_MEASURED_RUNS:-3}"
BENCHMARK_OFFLOAD="${BENCHMARK_OFFLOAD:-cpu}"
BENCHMARK_PROMPT="${BENCHMARK_PROMPT:-A cinematic historical shot with deliberate subject motion and a slow camera push.}"
BENCHMARK_QUANTIZATION="${BENCHMARK_QUANTIZATION:-fp8-cast}"
BENCHMARK_WARMUP_RUNS="${BENCHMARK_WARMUP_RUNS:-1}"

export BENCHMARK_CASE
export BENCHMARK_HARDWARE
export BENCHMARK_MEASURED_RUNS
export BENCHMARK_OFFLOAD
export BENCHMARK_PROMPT
export BENCHMARK_QUANTIZATION
export BENCHMARK_WARMUP_RUNS

mkdir -p "${RESULTS_ROOT}" "${MODELS_ROOT}"
: >"${LOG_PATH}"

write_status() {
    local status="$1"
    local message="$2"
    "${PYTHON}" - "${STATUS_PATH}" "${status}" "${message}" <<'PY'
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

path = Path(sys.argv[1])
document = {
    "schema_version": "1",
    "status": sys.argv[2],
    "message": sys.argv[3],
    "updated_at": datetime.now(UTC).isoformat(),
    "hardware": os.environ.get("BENCHMARK_HARDWARE", "rtx5090"),
    "case": os.environ.get("BENCHMARK_CASE", "distilled-fp8-cpu"),
}
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
temporary.replace(path)
PY
}

SERVER_PID=""

cleanup() {
    if [[ -n "${SERVER_PID}" ]]; then
        kill "${SERVER_PID}" 2>/dev/null || true
    fi
}

on_signal() {
    trap - ERR
    cleanup
    exit 0
}

on_error() {
    local exit_code="$?"
    write_status "failed" "Benchmark command failed with exit code ${exit_code}."
    echo "benchmark failed exit_code=${exit_code}" | tee -a "${LOG_PATH}"
    exit "${exit_code}"
}

trap cleanup EXIT
trap on_signal INT TERM
trap on_error ERR

write_status "starting" "Starting result server and validating the GPU runtime."
"${PYTHON}" "${FACTORY}/result_server.py" >>"${LOG_PATH}" 2>&1 &
SERVER_PID="$!"

for _ in $(seq 1 30); do
    if curl --fail --silent --show-error http://127.0.0.1:8080/health >/dev/null; then
        break
    fi
    sleep 1
done
curl --fail --silent --show-error http://127.0.0.1:8080/health >/dev/null

{
    echo "CHECK_1_TORCH_IMPORT"
    "${PYTHON}" -X faulthandler -c \
        'import torch; print("torch=", torch.__version__); print("torch_cuda=", torch.version.cuda)'

    echo "CHECK_2_CUDA"
    "${PYTHON}" -X faulthandler -c \
        'import torch; assert torch.cuda.is_available(); print("gpu=", torch.cuda.get_device_name(0)); print("capability=", torch.cuda.get_device_capability(0)); x=torch.ones(1024, device="cuda"); print("cuda_sum=", x.sum().item())'

    echo "CHECK_3_NATTEN"
    "${PYTHON}" -X faulthandler -c \
        'import natten; print("natten=", getattr(natten, "__version__", "unknown"))'

    echo "CHECK_4_LTX_IMPORT"
    "${PYTHON}" -X faulthandler -c \
        'import ltx_pipelines.distilled; print("ltx_pipeline=OK")'

    test -s "${KEYFRAME}"
    echo "DIAGNOSTIC_OK"
} 2>&1 | tee -a "${LOG_PATH}"

write_status "downloading_models" "Downloading the five LTX-2.5 model files sequentially."

MODEL_FILES=(
    diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
    text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
    vae/ltx-2.5-video-vae-bf16.safetensors
    vae/ltx-2.5-audio-vae-bf16.safetensors
    latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
)

for model_file in "${MODEL_FILES[@]}"; do
    echo "MODEL_DOWNLOAD_START ${model_file}" | tee -a "${LOG_PATH}"
    hf download \
        "${MODEL_REPOSITORY}" \
        "${model_file}" \
        --local-dir "${MODELS_ROOT}" \
        2>&1 | tee -a "${LOG_PATH}"
    echo "MODEL_DOWNLOAD_DONE ${model_file}" | tee -a "${LOG_PATH}"
done

write_status "running" "Running the canonical RTX 5090 benchmark case."

"${PYTHON}" "${FACTORY}/scripts/run_phase7_benchmark_matrix.py" \
    --hardware-label "${BENCHMARK_HARDWARE}" \
    --pipeline distilled \
    --prompt "${BENCHMARK_PROMPT}" \
    --conditioning-image "${KEYFRAME}" \
    --seed 42 \
    --width 768 \
    --height 1280 \
    --num-frames 121 \
    --fps 24 \
    --warmup-runs "${BENCHMARK_WARMUP_RUNS}" \
    --measured-runs "${BENCHMARK_MEASURED_RUNS}" \
    --case "${BENCHMARK_CASE}:${BENCHMARK_QUANTIZATION}:${BENCHMARK_OFFLOAD}" \
    --output-dir "${RESULTS_ROOT}" \
    --temp-dir "${RESULTS_ROOT}" \
    -- \
    "${PYTHON}" -m ltx_pipelines.distilled \
    --transformer-path "${MODELS_ROOT}/diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors" \
    --text-encoder-path "${MODELS_ROOT}/text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors" \
    --video-vae-path "${MODELS_ROOT}/vae/ltx-2.5-video-vae-bf16.safetensors" \
    --audio-vae-path "${MODELS_ROOT}/vae/ltx-2.5-audio-vae-bf16.safetensors" \
    --spatial-upsampler-path "${MODELS_ROOT}/latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors" \
    --prompt '{prompt}' \
    --image '{conditioning_image}' 0 1.0 \
    --width 768 \
    --height 1280 \
    --num-frames 121 \
    --frame-rate 24 \
    --seed '{seed}' \
    '{quantization_args}' \
    '{offload_args}' \
    --output-path '{output}' \
    2>&1 | tee -a "${LOG_PATH}"

write_status "succeeded" "Benchmark completed; results are ready to download."
echo "BENCHMARK_SUCCEEDED" | tee -a "${LOG_PATH}"

trap - ERR
wait "${SERVER_PID}"
