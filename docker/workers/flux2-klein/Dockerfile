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
    HF_HUB_OFFLINE=1 \
    HF_HUB_DOWNLOAD_TIMEOUT=120 \
    HF_HUB_ETAG_TIMEOUT=30 \
    HF_XET_HIGH_PERFORMANCE=1 \
    INFERENCE_WORKER_MODE=production \
    INFERENCE_WORKER_TEMP_DIR=/tmp/ai-video-factory \
    FLUX2_KLEIN_MODEL_ROOT=/workspace/models/flux2-klein-4b \
    FLUX2_KLEIN_MODEL_REPOSITORY=black-forest-labs/FLUX.2-klein-4B \
    FLUX2_KLEIN_MODEL_REVISION=e7b7dc27f91deacad38e78976d1f2b499d76a294 \
    FLUX2_KLEIN_DEVICE=cuda \
    FLUX2_KLEIN_DTYPE=bfloat16 \
    FLUX2_KLEIN_DOWNLOAD_STALL_TIMEOUT_SECONDS=600 \
    FLUX2_KLEIN_DOWNLOAD_HARD_TIMEOUT_SECONDS=2400 \
    FLUX2_KLEIN_DOWNLOAD_POLL_SECONDS=15 \
    FLUX2_KLEIN_DOWNLOAD_MIN_MIBPS=6 \
    FLUX2_KLEIN_DOWNLOAD_THROUGHPUT_GRACE_SECONDS=180 \
    FLUX2_KLEIN_DOWNLOAD_THROUGHPUT_WINDOW_SECONDS=120 \
    FLUX2_KLEIN_DOWNLOAD_REALLOCATE_ON_SLOW=true \
    FLUX2_KLEIN_READY_TIMEOUT_SECONDS=3600 \
    SALAD_QUEUE_ENABLED=false

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/factory
COPY pyproject.toml README.md /opt/factory/
COPY src /opt/factory/src

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        '/opt/factory[gpu]' \
        'diffusers==0.40.0' \
        'transformers==5.17.0' \
        'accelerate>=1.10,<2' \
        'sentencepiece>=0.2,<0.3' \
        'protobuf>=5,<7' \
        'huggingface-hub>=1.0,<2' \
        'hf-xet>=1.1,<2'

COPY --from=salad-worker /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/
COPY docker/workers/flux2-klein/download_models.sh /usr/local/bin/download-models
COPY docker/workers/flux2-klein/entrypoint.sh /usr/local/bin/worker-entrypoint

RUN chmod 0755 \
        /usr/local/bin/salad-http-job-queue-worker \
        /usr/local/bin/download-models \
        /usr/local/bin/worker-entrypoint \
    && useradd --create-home --uid 10001 worker \
    && mkdir --parents \
        /workspace/models/flux2-klein-4b \
        /workspace/.cache/huggingface \
        /tmp/ai-video-factory \
        /opt/factory/data/inference-local \
    && chown --recursive worker:worker \
        /workspace \
        /tmp/ai-video-factory \
        /opt/factory/data

RUN python3 -c \
    "import diffusers, sentencepiece, torch, transformers; \
     print('torch=', torch.__version__); \
     print('cuda=', torch.version.cuda); \
     print('diffusers=', diffusers.__version__); \
     print('transformers=', transformers.__version__); \
     print('sentencepiece=', sentencepiece.__version__); \
     from diffusers import Flux2KleinPipeline; \
     print('flux2_klein_pipeline=', Flux2KleinPipeline.__name__)"

USER worker
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

ENTRYPOINT ["worker-entrypoint"]
