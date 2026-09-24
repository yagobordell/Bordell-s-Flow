FROM python:3.12-slim-bookworm AS salad-worker
ARG SALAD_WORKER_VERSION=v0.7.0
ARG SALAD_WORKER_SHA256=074a329cf6462e77fc7b72100f59d8a690831456d9420186a834a8f30634c9e4
RUN apt-get update && apt-get install --yes --no-install-recommends ca-certificates curl \
 && curl --fail --show-error --location "https://github.com/SaladTechnologies/salad-cloud-job-queue-worker/releases/download/${SALAD_WORKER_VERSION}/salad-http-job-queue-worker_x86_64.tar.gz" --output /tmp/salad-worker.tar.gz \
 && echo "${SALAD_WORKER_SHA256}  /tmp/salad-worker.tar.gz" | sha256sum --check --strict \
 && tar --extract --gzip --file /tmp/salad-worker.tar.gz --directory /usr/local/bin salad-http-job-queue-worker \
 && chmod 0755 /usr/local/bin/salad-http-job-queue-worker \
 && rm -f /tmp/salad-worker.tar.gz \
 && rm -rf /var/lib/apt/lists/*

FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DEFAULT_TIMEOUT=300 \
 HF_HOME=/workspace/.cache/huggingface XDG_CACHE_HOME=/workspace/.cache HF_HUB_OFFLINE=1 \
 HF_HUB_DOWNLOAD_TIMEOUT=120 HF_HUB_ETAG_TIMEOUT=30 \
 INFERENCE_WORKER_MODE=production INFERENCE_WORKER_TEMP_DIR=/tmp/ai-video-factory \
 QWEN_IMAGE_21_MODEL_ROOT=/workspace/models/qwen-image-2.1 \
 QWEN_IMAGE_21_MODEL_REPOSITORY=Qwen/Qwen-Image-2.1 \
 QWEN_IMAGE_21_MODEL_REVISION=b3179ad355be050328e483a9dfdd9e60cd62adfa \
 QWEN_IMAGE_21_DEVICE=cuda SALAD_QUEUE_ENABLED=false
RUN apt-get update && apt-get install --yes --no-install-recommends ca-certificates curl git python3-pip && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/factory
COPY pyproject.toml README.md /opt/factory/
COPY src /opt/factory/src
RUN python3 -m pip install --break-system-packages --no-cache-dir '/opt/factory[gpu]' \
 'transformers==5.17.0' 'bitsandbytes>=0.49,<1' 'accelerate>=1.11,<2' 'sentencepiece>=0.2,<0.3' 'protobuf>=5,<7' \
 'huggingface-hub>=1.32,<2' 'hf-xet>=1.6,<2' 'git+https://github.com/huggingface/diffusers.git@0121a91f9d419ff7234c8a5923f82c244e6f1914'
COPY --from=salad-worker /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/
COPY docker/workers/qwen-image-2.1/download_models.sh /usr/local/bin/download-models
COPY docker/workers/common/entrypoint.sh /usr/local/bin/common-worker-entrypoint
COPY docker/workers/common/network_preflight.sh /usr/local/bin/network-preflight
COPY docker/workers/qwen-image-2.1/entrypoint.sh /usr/local/bin/worker-entrypoint
RUN chmod 0755 /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/download-models /usr/local/bin/common-worker-entrypoint \
        /usr/local/bin/network-preflight /usr/local/bin/worker-entrypoint \
 && useradd --create-home --uid 10001 worker \
 && mkdir --parents /workspace/models/qwen-image-2.1 /workspace/.cache/huggingface /tmp/ai-video-factory /opt/factory/data/inference-local \
 && chown --recursive worker:worker /workspace /tmp/ai-video-factory /opt/factory/data
RUN python3 -c "import torch, transformers; from diffusers import QwenImage21Pipeline; print('torch=', torch.__version__); print('cuda=', torch.version.cuda); print('transformers=', transformers.__version__); print('pipeline=', QwenImage21Pipeline.__name__)"
USER worker
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10m --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]
ENTRYPOINT ["worker-entrypoint"]
