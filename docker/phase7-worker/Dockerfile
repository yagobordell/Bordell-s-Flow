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

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GPU_WORKER_MODE=production \
    GPU_WORKER_TEMP_DIR=/tmp/ai-video-factory \
    SALAD_QUEUE_ENABLED=false

WORKDIR /app

COPY --from=salad-worker /usr/local/bin/salad-http-job-queue-worker /usr/local/bin/
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir '.[gpu]'

COPY docker/phase7-worker/entrypoint.sh /usr/local/bin/phase7-entrypoint
RUN chmod 0755 /usr/local/bin/phase7-entrypoint \
    && useradd --create-home --uid 10001 worker \
    && mkdir --parents /tmp/ai-video-factory /app/data/phase7-local \
    && chown --recursive worker:worker /tmp/ai-video-factory /app/data

USER worker
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3)"]

ENTRYPOINT ["phase7-entrypoint"]
