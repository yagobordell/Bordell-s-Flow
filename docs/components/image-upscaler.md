# OptiumLabs image upscaler

Bordell consumes the OptiumLabs image-upscaler as an external HTTP service. It does
not copy or modify OptiumLabs Real-ESRGAN code and it does not replace the existing
Real-ESRGAN video worker.

## Salad adapter

The adapter lives in `docker/services/image-upscaler-salad` and is pinned to the
verified OCI source digest:

`ghcr.io/yagobordell/image-upscaler@sha256:00e046a7ba6dd00d406609f2f5c007b176b96578b1baac612ea8e97940c35aba`

The original runtime is configured with `RUNTIME_HOST=127.0.0.1` and
`RUNTIME_PORT=8081`. `socat` exposes TCP6 port 8080 and forwards it to the
original IPv4 runtime.

Validate and publish the private derived package with the service manager:

```powershell
pwsh -File scripts/salad/manage_image_upscaler_service.ps1 -Action Validate
pwsh -File scripts/salad/manage_image_upscaler_service.ps1 -Action Publish
```

Deploy the immutable `repo@sha256:...` image reference, not a mutable tag:

```powershell
pwsh -File scripts/salad/manage_image_upscaler_service.ps1 `
  -Action Deploy `
  -PinnedImage "ghcr.io/yagobordell/image-upscaler-salad@sha256:<digest>"
```

The deployer uses `SALAD_ORGANIZATION`, `SALAD_PROJECT`, `SALAD_API_KEY`, and
`GHCR_READ_PACKAGES_PAT`. The GHCR credential used by Salad should be a classic
PAT restricted to `read:packages`; it is an operator/deployment credential and is
not required by the Bordell runtime after the Container Group is configured.

## Runtime client

Configure `IMAGE_UPSCALER_URL` and `SALAD_API_KEY` and reuse the existing R2 settings:

- `R2_ENDPOINT_URL`
- `R2_BUCKET`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`

`OptiumLabsImageUpscaler` uploads a request-scoped input, creates signed GET/PUT URLs,
builds `runtime-http/v1`, hashes canonical JSON after replacing `get_url` and `put_url`
values with `<ephemeral-access-capability>`, sends `Salad-Api-Key`, validates response
identity plus SHA/size/dimensions/PNG format, and returns output bytes.

The upstream runtime serves HTTP serially. Long Real-ESRGAN execution can therefore block its
own health/readiness handlers. The Salad manifest gives readiness and liveness enough failure
budget for long inference, and the Bordell client recovers transient Gateway `503`/`524`
responses while preserving and reusing the same deterministic R2 output request.

The existing video Real-ESRGAN worker is intentionally independent.

## Real inference smoke

Health/readiness do not prove model execution. After the Container Group is deployed, run:

```powershell
uv run --locked --extra dev python scripts/smoke/run_image_upscaler_smoke.py --case small
uv run --locked --extra dev python scripts/smoke/run_image_upscaler_smoke.py --case real
```

The real validated cases are `640x360 -> 1920x1080` and
`1280x720 -> 3840x2160`. Both exercise the full R2 signed-URL flow through
`POST /v1/execute`.
