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

Professional B2 `media_image` and `avatar_media` beats are generated
at their selected B2 size and quality; then OptiumLabs runs exact **2x**
on static beats, including verified static grid crops. Thus a normal
`1280x720` still becomes `2560x1440`, while a static panel extracted
from a high-resolution mixed 2x2 grid (`1920x1080`) becomes
`3840x2160`. The `768x864` avatar_media support panels preserve their
separate 8:9 geometry and become `1536x1728`.

The generation quality policy is uniform across professional and Casero:
**medium for full grids and individual prompts containing a case-insensitive
`(For context` marker with optional whitespace; low otherwise.** In
particular, independently generated professional I2V/A2V keyframes
(`media_video` and regenerated `avatar`) use `1920x1088` at **low**
unless they include `For context`. These independent keyframes are
center-cropped by four rows per side to `1920x1080`; reused user-avatar
PNGs remain unchanged.

**No grid panel is ever regenerated with GPT.** A professional grid
containing only still images retains its normal 16:9 panel geometry:
2x2 `2560x1440`, 3x2 `3840x1440`, 3x3 `3840x2160`; each panel
is nominally `1280x720`. When **any** participating panel is intended
for video (including an unnumbered filler), a 2x2 grid uses
`3840x2160` so each panel is nominally `1920x1080`. Larger 3x2 and
3x3 grids are already at the supported canvas limit and therefore retain
`1280x720` panels. All grids use medium. Exclusive avatar_media grids
retain their special 8:9 sizes.

After cropping, grid-video panels are used directly as the video source,
**at the cropped size**: `1920x1080` for supported mixed 2x2 grids and
`1280x720` for larger grids. They receive neither an independent GPT
edit nor upscaling to disguise a lower resolution. Only grid-photo panels
receive the regular exact-2x OptiumLabs finishing. The source full grid
remains immutable. No new `native_grid_video` paid request or finishing
state is created. Older ambiguous native-edit states are rejected and
retained for manual reconciliation, never silently resubmitted or deleted;
historical finished states remain available for cost auditing.

The professional finishing stage keeps exact 2x and standalone-video crop
results in their original beat paths (`images/block_N/BEAT.png`), with
SHA-verified recovery metadata under `images/finishing_state/`.
Grid-video images already occupy their final per-beat paths and are
verified, not overwritten. The source full grids are preserved unchanged.
If Salad/R2 is unavailable, static-image finishing stays pending without
regenerating paid originals. Professional preview/publication requires all
finishing to complete. Existing experimental video inference and output
geometry remain independent of these source-image dimensions. Explicit B2
reruns now archive original paid GPT request JSON as well as paid finishing
metadata, preserving historical billing even when no native grid edit is
created.

To validate a deployed OptiumLabs service, use the real `small` and `real`
smokes above. Separately, perform a controlled new **professional B2**
end-to-end run and visually inspect mixed-grid continuity and the two
high-resolution video references before releasing to production. Offline CI
cannot prove visual fidelity or service availability.
