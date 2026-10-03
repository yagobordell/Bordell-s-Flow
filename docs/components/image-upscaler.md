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

Configure `IMAGE_UPSCALER_URL` in `config/salad.env`, keep `SALAD_API_KEY` in `.env`,
and reuse the existing R2 settings:

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

## Professional B2 image finishing (new opt-in by visual style)

Professional B2 `media_image` and `avatar_media` beats keep their original
GPT Image generation size, quality, prompt and original paid PNG. **Only after**
individual generation or verified grid-panel cropping, the OptiumLabs Salad
image service runs **exact 2x** on each such beat:
`1280x720 -> 2560x1440` and `768x864 -> 1536x1728` respectively.
The service is mandatory to **finish** professional photo beats, but Salad
availability does not gate original GPT Image generation. Paid original PNGs and
verified grid crops are saved first; then Salad is started/reused and the x2
finishing runs. If Salad/R2 is unavailable, the manifest records
`finishing_status=pending_upscaler` and `--images-only` reuses the saved paid
originals instead of charging for them again. Preview/video publishing still
requires completed professional finishing. This never affects Casero.

Images destined for professional I2V or newly generated speaking A2V source
frames (`media_video` and generated `avatar`) are **native GPT Image**
`1920x1088`, medium quality. Both values are divisible by 16. The final image
is deterministically center-cropped, not resized, by four rows on each side
to **1920x1080**. The supplied, reused avatar PNG is not a newly generated
frame and remains the user's original. A2V and I2V **video inference and
final montage remain 1280x720**; only their source images have changed.

A professional grid (including a mixed image/video grid) keeps the current
controlled canvas and medium-quality GPT generation. After cropping, photo
panels are upscaled x2 through OptiumLabs. Each video panel is additionally
rendered as a **separate paid native 1920x1088 GPT edit**, with its verified
grid crop as the first strict composition/continuity reference. If that panel
references an avatar, the original verified avatar is included as a second
reference. The native output is center-cropped to 1920x1080. This avoids
pretending that a 720p grid panel merely resized to 1080p is natively detailed,
and avoids giant 5760x3240 grids. The video requirement governs the final
video-panel dimensions. Paid edit costs are included once in B2 image billing.

Original GPT PNGs, original full grids and derived crops remain immutable.
Finishing uses per-beat JSON state, SHA-256 input/output verification and
distinct derived paths under `images/professional_finished/`; uncertain
remote outcomes are never resubmitted blindly. LTX and the preview consume
**only** the verified finished assets for professional runs. No video
Real-ESRGAN/upscaler is used.

To validate a deployed OptiumLabs service, use the real `small` and `real`
smokes above. Separately, perform a controlled new **professional B2**
end-to-end run and visually inspect mixed-grid continuity and the two
high-resolution video references before releasing to production. Offline CI
cannot prove visual fidelity or service availability.
