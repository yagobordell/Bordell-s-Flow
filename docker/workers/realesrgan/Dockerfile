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
    && chmod 0755 /usr/local/bin/salad-http-job-queue-worker \
    && rm -f /tmp/salad-worker.tar.gz \
    && rm -rf /var/lib/apt/lists/*

FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime

ARG REALESRGAN_MODEL_SHA256=49fafd45f8fd7aa8d31ab2a22d14d91b536c34494a5cfe31eb5d89c2fa266abb
ARG REALESRGAN_MODEL_URL=https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    INFERENCE_WORKER_MODE=production \
    INFERENCE_WORKER_TEMP_DIR=/tmp/ai-video-factory \
    REALESRGAN_MODEL_ROOT=/workspace/models/realesrgan \
    REALESRGAN_DEVICE=cuda \
    SALAD_QUEUE_ENABLED=false

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl ffmpeg python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/factory
COPY pyproject.toml README.md /opt/factory/
COPY src /opt/factory/src

RUN python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        '/opt/factory[gpu]' \
        'basicsr==1.4.2' \
        'realesrgan==0.3.0' \
    && python3 - <<'PY'
from pathlib import Path
import site
candidates = [Path(root) / "basicsr" / "data" / "degradations.py" for root in site.getsitepackages()]
path = next((candidate for candidate in candidates if candidate.is_file()), None)
if path is None:
    raise SystemExit("BasicSR degradations.py not found")
text = path.read_text()
text = text.replace(
    "from torchvision.transforms.functional_tensor import rgb_to_grayscale",
    "from torchvision.transforms.functional import rgb_to_grayscale",
)
path.write_text(text)
PY

RUN mkdir --parents /workspace/models/realesrgan \
    && curl --fail --show-error --location "${REALESRGAN_MODEL_URL}" \
        --output /workspace/models/realesrgan/RealESRGAN_x2plus.pth \
    && echo "${REALESRGAN_MODEL_SHA256}  /workspace/models/realesrgan/RealESRGAN_x2plus.pth" \
        | sha256sum --check --strict \
    && printf '%s\n' "RealESRGAN_x2plus@${REALESRGAN_MODEL_SHA256}" \
        > /workspace/models/realesrgan/.ready

COPY --from=salad-worker /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/
COPY docker/workers/realesrgan/entrypoint.sh /usr/local/bin/worker-entrypoint

RUN chmod 0755 /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/worker-entrypoint \
    && useradd --create-home --uid 10001 worker \
    && mkdir --parents /tmp/ai-video-factory /opt/factory/data/inference-local \
    && chown --recursive worker:worker /workspace /tmp/ai-video-factory /opt/factory/data

RUN python3 -c "import basicsr, realesrgan, torch; from basicsr.archs.rrdbnet_arch import RRDBNet; from realesrgan import RealESRGANer; print('torch=', torch.__version__); print('realesrgan=OK')"

USER worker
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=2m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

ENTRYPOINT ["worker-entrypoint"]
