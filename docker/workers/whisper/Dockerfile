FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    HF_HOME=/workspace/.cache/huggingface \
    XDG_CACHE_HOME=/workspace/.cache \
    HF_HUB_DOWNLOAD_TIMEOUT=120 \
    HF_HUB_ETAG_TIMEOUT=30 \
    HF_HUB_DISABLE_XET=1 \
    INFERENCE_WORKER_MODE=production \
    INFERENCE_WORKER_TEMP_DIR=/tmp/ai-video-factory \
    WHISPER_MODEL_ROOT=/workspace/models/whisper-large-v3-turbo \
    WHISPER_MODEL_REPOSITORY=openai/whisper-large-v3-turbo \
    WHISPER_MODEL_REVISION=main \
    WHISPER_DEVICE=cuda:0 \
    WHISPER_DTYPE=float16 

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
        'hf-xet==1.6.0' \
        'transformers==5.5.2'

COPY docker/workers/whisper/download_models.sh /usr/local/bin/download-models
COPY docker/workers/common/entrypoint.sh /usr/local/bin/common-worker-entrypoint
COPY docker/workers/whisper/entrypoint.sh /usr/local/bin/worker-entrypoint

RUN chmod 0755 \
        /usr/local/bin/download-models \
        /usr/local/bin/common-worker-entrypoint \
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
     from importlib.metadata import version; \
     assert torch.__version__.startswith('2.11.0'), torch.__version__; \
     assert torch.version.cuda == '12.8', torch.version.cuda; \
     assert transformers.__version__ == '5.5.2', transformers.__version__; \
     assert version('hf-xet') == '1.6.0', version('hf-xet'); \
     print('torch=', torch.__version__); \
     print('cuda=', torch.version.cuda); \
     print('transformers=', transformers.__version__); \
     print('hf-xet=', version('hf-xet'))"

USER worker
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=3m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

ENTRYPOINT ["worker-entrypoint"]
