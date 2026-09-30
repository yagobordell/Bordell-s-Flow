# OptiumLabs image upscaler

Bordell consumes the OptiumLabs image-upscaler as an external HTTP service. It does
not copy or modify OptiumLabs Real-ESRGAN code and it does not replace the existing
Real-ESRGAN video worker.

## Salad adapter

The adapter lives in docker/services/image-upscaler-salad and is pinned to the
verified OCI source digest:

ghcr.io/yagobordell/image-upscaler@sha256:00e046a7ba6dd00d406609f2f5c007b176b96578b1baac612ea8e97940c35aba

The original runtime is configured with RUNTIME_HOST=127.0.0.1 and
RUNTIME_PORT=8081. socat exposes TCP6 port 8080 and forwards it to the original
IPv4 runtime.

Build and publish the derived package privately, then resolve its immutable digest:

    docker build -f docker/services/image-upscaler-salad/Dockerfile -t ghcr.io/yagobordell/image-upscaler-salad:<tag> .
    docker push ghcr.io/yagobordell/image-upscaler-salad:<tag>
    docker buildx imagetools inspect ghcr.io/yagobordell/image-upscaler-salad:<tag>

Deploy the digest, not the mutable tag:

    .\scripts\salad\deploy_image_upscaler.ps1 -Image ghcr.io/yagobordell/image-upscaler-salad@sha256:<digest>

The deployer requires SALAD_ORGANIZATION, SALAD_PROJECT, SALAD_API_KEY and
GHCR_READ_PAT. The GHCR token used by Salad should be a classic PAT restricted to
read:packages.

## Runtime client

Configure IMAGE_UPSCALER_URL and SALAD_API_KEY and reuse the existing R2 settings.
OptiumLabsImageUpscaler uploads a request-scoped input, creates signed GET/PUT URLs,
builds runtime-http/v1, hashes canonical JSON after replacing get_url and put_url
values with <ephemeral-access-capability>, sends Salad-Api-Key, validates response
identity plus SHA/size/dimensions/PNG format, and returns output bytes.

The existing video Real-ESRGAN worker is intentionally independent.

## Real inference smoke

Health/readiness do not prove model loading. After the Container Group is deployed,
run:

    uv run --locked --extra dev python scripts/smoke/run_image_upscaler_smoke.py --case all

The smoke runs 640x360 -> 1920x1080 first, then 1280x720 -> 3840x2160.
