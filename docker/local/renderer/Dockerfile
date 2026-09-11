FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libnss3 \
        libdbus-1-3 \
        libatk1.0-0 \
        libgbm-dev \
        libasound2 \
        libxrandr2 \
        libxkbcommon-dev \
        libxfixes3 \
        libxcomposite1 \
        libxdamage1 \
        libatk-bridge2.0-0 \
        libpango-1.0-0 \
        libcairo2 \
        libcups2 \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

COPY scripts ./scripts
COPY remotion/package.json remotion/tsconfig.json ./remotion/
COPY remotion/src ./remotion/src
COPY remotion/public ./remotion/public

WORKDIR /workspace/remotion
RUN npm install --no-audit --no-fund \
    && npx remotion browser ensure \
    && npm cache clean --force

WORKDIR /workspace
COPY docker/local/renderer/render_phase9.sh /usr/local/bin/render-phase9
RUN chmod +x /usr/local/bin/render-phase9

CMD ["/usr/local/bin/render-phase9"]
