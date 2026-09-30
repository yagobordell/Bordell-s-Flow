# OptiumLabs image upscaler on Salad

This integration consumes the OptiumLabs image-upscaler as an external HTTP service. It does not
modify OptiumLabs code, copy its Real-ESRGAN implementation, or attach the service to Bordell's
Postgres GPU-worker control plane.

The immutable upstream artifact is:

`ghcr.io/yagobordell/image-upscaler@sha256:00e046a7ba6dd00d406609f2f5c007b176b96578b1baac612ea8e97940c35aba`

The existing `realesrgan` worker remains the video 2x capability and is not replaced by this service.

## Salad adapter

`docker/services/image-upscaler-salad/Dockerfile` derives from the immutable upstream digest.
It only installs `socat` and changes the runtime binding through the upstream-supported
`RUNTIME_HOST` and `RUNTIME_PORT` variables:

- original runtime: `127.0.0.1:8081`
- Salad-facing listener: `[::]:8080`
- bridge: `socat TCP6-LISTEN:8080,reuseaddr,fork TCP4:127.0.0.1:8081`

No OptiumLabs source files are copied or patched.

Validate the committed adapter and service manifest:

```powershell
pwsh -File scripts/salad/manage_image_upscaler_service.ps1 -Action Validate
```

## Publish the private GHCR adapter

Authenticate Docker to GHCR with credentials that can publish the Bordell package, then run:

```powershell
pwsh -File scripts/salad/manage_image_upscaler_service.ps1 -Action Publish
```

The script builds `linux/amd64`, pushes
`ghcr.io/yagobordell/image-upscaler-salad:salad-v1`, resolves the published digest, and prints the
immutable `repo@sha256:...` reference. Keep the GHCR package private.

## Deploy the independent Container Group

The deployment manifest fixes the requested production shape:

- one replica
- RTX 5090 (32 GB)
- 4 CPU
- 16 GiB RAM
- HTTP Container Gateway on port 8080
- Gateway authentication enabled
- startup `GET /health`
- readiness `GET /ready`, every 30 seconds, failure threshold 20
- liveness `GET /health`, every 60 seconds, failure threshold 20

The upstream runtime uses a serial HTTP server. While a long `POST /v1/execute` is running,
health/readiness requests may have to wait behind that inference. The wider readiness/liveness
budgets prevent Salad from evicting an otherwise healthy instance during a long Real-ESRGAN run.

Set or provide these operator settings:

```text
SALAD_API_KEY
SALAD_ORGANIZATION
SALAD_PROJECT
GHCR_READ_PACKAGES_PAT
```

`GHCR_READ_PACKAGES_PAT` must be a classic GitHub PAT restricted to `read:packages` for the
private image pull. The deployment script passes it only to Salad's private-registry
authentication payload and does not commit it to the repository.

Deploy by immutable digest:

```powershell
pwsh -File scripts/salad/manage_image_upscaler_service.ps1 `
  -Action Deploy `
  -PinnedImage "ghcr.io/yagobordell/image-upscaler-salad@sha256:<digest>"
```

If `-PinnedImage` is omitted, the script resolves the current `salad-v1` tag to a digest before
submitting the Container Group.

Read the group status and Gateway URL:

```powershell
pwsh -File scripts/salad/manage_image_upscaler_service.ps1 -Action Status
```

Use the printed Gateway URL as `IMAGE_UPSCALER_URL`.

## Bordell runtime configuration

Bordell reuses its existing R2 credentials and adds only the upscaler URL. The relevant settings
are:

```text
IMAGE_UPSCALER_URL=https://<salad-container-gateway-host>
SALAD_API_KEY=<salad-api-key>
R2_ENDPOINT_URL=<existing-r2-endpoint>
R2_BUCKET=<existing-r2-bucket>
R2_ACCESS_KEY_ID=<existing-r2-key>
R2_SECRET_ACCESS_KEY=<existing-r2-secret>
```

`OptiumLabsImageUpscaler` receives image bytes plus exact target width/height. For each call it
uploads a temporary input to R2, creates signed GET/PUT capabilities, sends the frozen
`runtime-http/v1` contract to `POST /v1/execute`, downloads the realized output, verifies the
runtime evidence and exact PNG dimensions, returns the bytes, and best-effort deletes the
temporary R2 objects.

Gateway requests include:

```text
Salad-Api-Key: <SALAD_API_KEY>
```

The `request_sha256` is computed from canonical JSON of the capability request after recursively
replacing every `get_url` and `put_url` value with
`"<ephemeral-access-capability>"`.

### Gateway timeout recovery

Long GPU inference can outlive the synchronous Container Gateway request. The client treats HTTP
`524` as an ambiguous in-flight execution: it preserves the deterministic request-scoped R2
objects, waits for the output to appear, and retries the same Runtime HTTP envelope to recover
the result evidence through the upstream idempotent output path.

HTTP `503` is also treated as transient during that recovery window. This is important because
the serial upstream HTTP server can temporarily make the instance unavailable to the Gateway
while a previous inference is still occupying it. Retries keep the same execution envelope and
do not create a different semantic image-upscale request.

Non-transient HTTP errors still fail immediately, and ambiguous failures preserve the R2 objects
so a later retry can reconcile the same deterministic output safely.

## Real inference smoke

A successful `/ready` response is not sufficient because the model is loaded on first inference.
Run the small real case first:

```powershell
uv run --locked --extra dev python scripts/smoke/run_image_upscaler_smoke.py --case small
```

This exercises:

`640x360 -> 1920x1080`

Then run the production-sized case:

```powershell
uv run --locked --extra dev python scripts/smoke/run_image_upscaler_smoke.py --case real
```

This exercises:

`1280x720 -> 3840x2160`

Running without `--case` executes both in that order. The smoke performs the full R2 signed-URL
flow, calls `/v1/execute`, downloads the output, opens it as an image, and requires the exact
requested dimensions.

## Validated deployment

The integration was validated against the independent Salad Container Group with authenticated
Gateway access. The real smoke cases completed successfully for both target sizes:

- `640x360 -> 1920x1080`
- `1280x720 -> 3840x2160`

Both tests exercised the real R2 signed-URL flow and recovered successfully from a Gateway
`524` while the GPU inference continued.
