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

FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    HF_HOME=/workspace/.cache/huggingface \
    XDG_CACHE_HOME=/workspace/.cache \
    HF_HUB_DISABLE_XET=1 \
    HF_HUB_ENABLE_HF_TRANSFER=0 \
    INFERENCE_WORKER_MODE=production \
    INFERENCE_WORKER_TEMP_DIR=/tmp/ai-video-factory \
    WHISPER_MODEL_ROOT=/workspace/models/whisper-large-v3-turbo \
    WHISPER_MODEL_REPOSITORY=openai/whisper-large-v3-turbo \
    WHISPER_MODEL_REVISION=main \
    WHISPER_DEVICE=cuda:0 \
    WHISPER_DTYPE=float16 \
    SALAD_QUEUE_ENABLED=false

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        ffmpeg \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -c \
    "import sys, torch, torchaudio; \
     assert sys.version_info >= (3, 12), sys.version; \
     assert torch.__version__.startswith('2.11.0'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda; \
     assert torchaudio.__version__.startswith('2.11.0'), torchaudio.__version__"

WORKDIR /opt/factory
COPY pyproject.toml README.md /opt/factory/
COPY src /opt/factory/src

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        '/opt/factory[gpu]' \
        'huggingface-hub[cli]' \
        'transformers==5.5.2'

COPY --from=salad-worker /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/
COPY docker/workers/whisper/download_models.sh /usr/local/bin/download-models
COPY docker/workers/whisper/entrypoint.sh /usr/local/bin/worker-entrypoint

RUN chmod 0755 \
        /usr/local/bin/salad-http-job-queue-worker \
        /usr/local/bin/download-models \
        /usr/local/bin/worker-entrypoint \
    && useradd --create-home --uid 10001 worker \
    && mkdir --parents \
        /workspace/models/whisper-large-v3-turbo \
        /workspace/.cache/huggingface \
        /tmp/ai-video-factory \
        /opt/factory/data/inference-local \
    && chown --recursive worker:worker \
        /workspace \
        /tmp/ai-video-factory \
        /opt/factory/data

RUN python3 -c \
    "import torch, transformers; \
     assert torch.__version__.startswith('2.11.0'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda; \
     assert transformers.__version__ == '5.5.2', transformers.__version__; \
     print('torch=', torch.__version__); \
     print('cuda=', torch.version.cuda); \
     print('transformers=', transformers.__version__)"

USER worker
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=3m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

ENTRYPOINT ["worker-entrypoint"]
