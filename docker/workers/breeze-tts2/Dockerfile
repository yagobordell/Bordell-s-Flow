FROM python:3.12-slim-bookworm AS salad-worker

ARG SALAD_WORKER_VERSION=v0.7.0
ARG SALAD_WORKER_SHA256=074a329cf6462e77fc7b72100f59d8a690831456d9420186a834a8f30634c9e4

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && curl --fail --show-error --location \
        "https://github.com/SaladTechnologies/salad-cloud-job-queue-worker/releases/download/${SALAD_WORKER_VERSION}/salad-http-job-queue-worker_x86_64.tar.gz" \
        --output /tmp/salad-worker.tar.gz \
    && echo "${SALAD_WORKER_SHA256}  /tmp/salad-worker.tar.gz" | sha256sum --check --strict \
    && tar --extract --gzip --file /tmp/salad-worker.tar.gz --directory /usr/local/bin \
        salad-http-job-queue-worker \
    && chmod 0755 /usr/local/bin/salad-http-job-queue-worker

FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04

ARG BREEZE_RUNTIME_COMMIT=008f769016b0a24711becd7a4925030bc93f608c
ARG FLASH_ATTN_CUDA_ARCHS=89

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    TOKENIZERS_PARALLELISM=false \
    HF_HOME=/workspace/.cache/huggingface \
    XDG_CACHE_HOME=/workspace/.cache \
    HF_HUB_DISABLE_XET=1 \
    HF_HUB_ENABLE_HF_TRANSFER=0 \
    PYTHONPATH=/opt/breeze-infer \
    INFERENCE_WORKER_MODE=production \
    INFERENCE_WORKER_TEMP_DIR=/tmp/ai-video-factory \
    BREEZE_MODEL_ROOT=/workspace/models/breeze-tts-2 \
    BREEZE_MODEL_REPOSITORY=BreezeBlue/Breeze-TTS-2 \
    BREEZE_MODEL_REVISION=main \
    BREEZE_RUNTIME_ROOT=/opt/breeze-infer \
    BREEZE_DEVICE=cuda \
    BREEZE_MAX_CHUNK_CHARS=1200 \
    BREEZE_INTER_CHUNK_PAUSE_MS=120 \
    SALAD_QUEUE_ENABLED=false

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        libsndfile1 \
        ninja-build \
        python3.12 \
        python3.12-dev \
        python3-pip \
        sox \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3

RUN python -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --upgrade pip setuptools wheel packaging ninja

RUN python -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu128 \
        torch==2.9.1 \
        torchaudio==2.9.1

RUN git clone https://github.com/breezeblue-ai/breeze-tts.git /opt/breeze-infer \
    && cd /opt/breeze-infer \
    && git checkout "${BREEZE_RUNTIME_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${BREEZE_RUNTIME_COMMIT}"

RUN python -m pip install \
        --break-system-packages \
        --no-cache-dir \
        -r /opt/breeze-infer/requirements.txt \
    && MAX_JOBS=8 FLASH_ATTN_CUDA_ARCHS=${FLASH_ATTN_CUDA_ARCHS} python -m pip install \
        --break-system-packages \
        --no-build-isolation \
        --no-cache-dir \
        --no-deps \
        flash-attn==2.8.3

WORKDIR /opt/factory
COPY pyproject.toml README.md /opt/factory/
COPY src /opt/factory/src

RUN python -m pip install \
        --break-system-packages \
        --no-cache-dir \
        '/opt/factory[gpu]' \
        'huggingface-hub[cli]'

COPY --from=salad-worker /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/
COPY docker/workers/breeze-tts2/download_models.sh /usr/local/bin/download-models
COPY docker/workers/breeze-tts2/entrypoint.sh /usr/local/bin/worker-entrypoint

RUN chmod 0755 \
        /usr/local/bin/salad-http-job-queue-worker \
        /usr/local/bin/download-models \
        /usr/local/bin/worker-entrypoint \
    && useradd --create-home --uid 10001 worker \
    && mkdir --parents \
        /workspace/models/breeze-tts-2 \
        /workspace/.cache/huggingface \
        /tmp/ai-video-factory \
        /opt/factory/data/inference-local \
    && chown --recursive worker:worker \
        /workspace \
        /tmp/ai-video-factory \
        /opt/factory/data

RUN python -c \
    "import sys, torch, torchaudio; \
     assert sys.version_info >= (3, 12), sys.version; \
     assert torch.__version__.startswith('2.9.1'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda; \
     assert torchaudio.__version__.startswith('2.9.1'), torchaudio.__version__; \
     import breeze_infer; \
     print('python=', sys.version); \
     print('torch=', torch.__version__); \
     print('cuda=', torch.version.cuda)"

USER worker
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

ENTRYPOINT ["worker-entrypoint"]
