FROM ghcr.io/yagobordell/image-upscaler@sha256:00e046a7ba6dd00d406609f2f5c007b176b96578b1baac612ea8e97940c35aba

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*

COPY docker/services/image-upscaler-salad/entrypoint.sh /usr/local/bin/image-upscaler-salad-entrypoint
RUN chmod 0755 /usr/local/bin/image-upscaler-salad-entrypoint

ENV RUNTIME_HOST=127.0.0.1 \
    RUNTIME_PORT=8081

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/image-upscaler-salad-entrypoint"]
