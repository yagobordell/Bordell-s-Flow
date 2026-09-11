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
    TORCH_CUDA_ARCH_LIST=12.0 \
    INFERENCE_WORKER_MODE=production \
    INFERENCE_WORKER_TEMP_DIR=/tmp/ai-video-factory \
    LTX_MODEL_ROOT=/workspace/models/ltx-2.5 \
    LTX_MODEL_REPOSITORY=Lightricks/LTX-2.5 \
    LTX_MODEL_REVISION=main \
    LTX_DEVICE=cuda \
    SALAD_QUEUE_ENABLED=false

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        git-lfs \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -c \
    "import sys, torch, torchvision, torchaudio; \
     assert sys.version_info >= (3, 12), sys.version; \
     assert torch.__version__.startswith('2.11.0'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda; \
     assert torchvision.__version__.startswith('0.26.0'), torchvision.__version__; \
     assert torchaudio.__version__.startswith('2.11.0'), torchaudio.__version__"

ARG LTX_SOURCE_COMMIT=a95ab856bf29407b6b066ede0abe1846050db56c

RUN git clone --filter=blob:none https://github.com/Lightricks/LTX-2.git /opt/ltx \
    && git -C /opt/ltx checkout "${LTX_SOURCE_COMMIT}"

RUN printf '%s\n' \
        'torch==2.11.0' \
        'torchvision==0.26.0' \
        'torchaudio==2.11.0' \
        > /opt/torch-constraints.txt

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --constraint /opt/torch-constraints.txt \
        --extra-index-url https://download.pytorch.org/whl/cu128 \
        --editable /opt/ltx/packages/ltx-core \
        --editable /opt/ltx/packages/ltx-pipelines

WORKDIR /opt/factory
COPY pyproject.toml README.md /opt/factory/
COPY src /opt/factory/src

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --constraint /opt/torch-constraints.txt \
        --extra-index-url https://download.pytorch.org/whl/cu128 \
        '/opt/factory[gpu]' \
        'huggingface-hub[cli]'

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        --no-deps \
        --find-links https://whl.natten.org \
        'natten==0.21.6+torch2110cu128'

COPY --from=salad-worker /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/
COPY docker/workers/ltx25/download_models.sh /usr/local/bin/download-models
COPY docker/workers/ltx25/entrypoint.sh /usr/local/bin/worker-entrypoint

RUN chmod 0755 \
        /usr/local/bin/salad-http-job-queue-worker \
        /usr/local/bin/download-models \
        /usr/local/bin/worker-entrypoint \
    && useradd --create-home --uid 10001 worker \
    && mkdir --parents \
        /workspace/models/ltx-2.5 \
        /workspace/.cache/huggingface \
        /tmp/ai-video-factory \
        /opt/factory/data/inference-local \
    && chown --recursive worker:worker \
        /workspace \
        /tmp/ai-video-factory \
        /opt/factory/data

RUN python3 -c \
    "import importlib.util, torch, ltx_pipelines.distilled; \
     assert torch.__version__.startswith('2.11.0'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda; \
     assert importlib.util.find_spec('natten') is not None; \
     import natten; \
     print('torch=', torch.__version__); \
     print('cuda=', torch.version.cuda); \
     print('natten=', natten.__version__); \
     print('ltx_pipeline=OK')"

USER worker
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

ENTRYPOINT ["worker-entrypoint"]
