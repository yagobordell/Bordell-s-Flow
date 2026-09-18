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

FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ARG FISH_RUNTIME_COMMIT=214da3cd841bda85da2496b96cd3c4d7edb1337e
ARG UV_VERSION=0.8.15

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    TOKENIZERS_PARALLELISM=false \
    HF_HOME=/workspace/.cache/huggingface \
    XDG_CACHE_HOME=/workspace/.cache \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DOWNLOAD_TIMEOUT=120 \
    HF_HUB_ETAG_TIMEOUT=30 \
    HF_XET_HIGH_PERFORMANCE=1 \
    VIRTUAL_ENV=/opt/fish-speech/.venv \
    PATH=/opt/fish-speech/.venv/bin:$PATH \
    PYTHONPATH=/opt/fish-speech \
    INFERENCE_WORKER_MODE=production \
    INFERENCE_WORKER_TEMP_DIR=/tmp/ai-video-factory \
    FISH_SPEECH_MODEL_ROOT=/workspace/models/fish-s2-pro \
    FISH_SPEECH_MODEL_REPOSITORY=fishaudio/s2-pro \
    FISH_SPEECH_MODEL_REVISION=1de9996b6be38b745688de084d87a5633f714e4e \
    FISH_SPEECH_RUNTIME_COMMIT=214da3cd841bda85da2496b96cd3c4d7edb1337e \
    FISH_SPEECH_DEVICE=cuda \
    FISH_SPEECH_MAX_CHUNK_BYTES=800 \
    FISH_SPEECH_INTER_CHUNK_PAUSE_MS=80 \
    SALAD_QUEUE_ENABLED=false

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        libasound2-dev \
        libportaudio2 \
        libsndfile1 \
        libsox-dev \
        portaudio19-dev \
        python3.12 \
        python3.12-dev \
        python3-pip \
        sox \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3 \
    && python3.12 -m pip install --break-system-packages --no-cache-dir "uv==${UV_VERSION}"

RUN git clone https://github.com/fishaudio/fish-speech.git /opt/fish-speech \
    && cd /opt/fish-speech \
    && git checkout "${FISH_RUNTIME_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${FISH_RUNTIME_COMMIT}" \
    && uv sync --python /usr/bin/python3.12 --extra cu128 --frozen --no-dev

WORKDIR /opt/factory
COPY pyproject.toml README.md /opt/factory/
COPY src /opt/factory/src

RUN uv pip install --python /opt/fish-speech/.venv/bin/python '/opt/factory[gpu]' \
    && uv pip install --python /opt/fish-speech/.venv/bin/python \
        'huggingface-hub==0.36.0' 'hf-xet==1.6.0'

COPY --from=salad-worker /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/
COPY docker/workers/fish-speech/download_models.sh /usr/local/bin/download-models
COPY docker/workers/fish-speech/entrypoint.sh /usr/local/bin/worker-entrypoint

RUN chmod 0755 \
        /usr/local/bin/salad-http-job-queue-worker \
        /usr/local/bin/download-models \
        /usr/local/bin/worker-entrypoint \
    && useradd --create-home --uid 10001 worker \
    && mkdir --parents \
        /workspace/models/fish-s2-pro \
        /workspace/.cache/huggingface \
        /tmp/ai-video-factory \
        /opt/factory/data/inference-local \
    && chown --recursive worker:worker \
        /workspace \
        /tmp/ai-video-factory \
        /opt/factory/data

RUN python -c \
    "import sys, torch, torchaudio; \
     from importlib.metadata import version; \
     from fish_speech.inference_engine import TTSInferenceEngine; \
     from tools.server.model_manager import ModelManager; \
     assert sys.version_info >= (3, 12), sys.version; \
     assert torch.__version__.startswith('2.8.0'), torch.__version__; \
     assert torchaudio.__version__.startswith('2.8.0'), torchaudio.__version__; \
     print('python=', sys.version); \
     print('torch=', torch.__version__); \
     print('cuda=', torch.version.cuda); \
     print('fish-speech=', version('fish-speech')); \
     print('manager=', ModelManager.__name__); \
     print('engine=', TTSInferenceEngine.__name__)"

USER worker
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

ENTRYPOINT ["worker-entrypoint"]
