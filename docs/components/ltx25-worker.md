# LTX 2.5 worker

LTX 2.5 is the production video-generation worker. It handles normal image-to-video shots and exposes
the audio-to-video capability used by avatar-oriented flows.

## Boundary

Normal Phase 8 generation receives a storyboard keyframe, motion prompt and timing-derived generation
parameters, then writes one silent MP4 artifact per shot.

Production video geometry is:

```text
1280x720
24 fps
silent H.264 MP4
```

Qwen text-to-image keyframes are 1280x736 rather than exact 16:9. For I2V, the worker
preserves the entire source frame using proportional fit and narrow edge-extended side margins
before padding the internal model grid to 1280x768. Only that internal grid padding is removed
from the decoded output. The public MP4 remains 1280x720; no Qwen content is center-cropped.

Accepted clips are subsequently upscaled by Real-ESRGAN to 2560x1440.

## Container and bootstrap

The image lives under `docker/workers/ltx25` and packages the pinned LTX runtime plus the shared
inference worker.

Required model checkpoints are downloaded under the configured model root. LTX reuses
`ai_video_factory.workers.download_watchdog` and pins both the LTX source commit and the
Hugging Face model revision. `docker/workers/ltx25/model-manifest.json` defines the exact
shared file set and optional dev-only assets. Bootstrap writes a provenance manifest with
file sizes and SHA256 values after validating/downloading from the pinned revision; a cache
without matching provenance is revalidated instead of being accepted merely because files
are non-empty.

The default worker bootstrap downloads only the shared distilled assets required by normal
I2V, legacy fast A2V and the reference A2V profile. The dev transformer and distilled LoRA
are optional comparison assets enabled explicitly with `LTX_INCLUDE_A2V_DEV_ASSETS=true`.

## Deployment

Build and publish the new LTX Docker image tag before deploying the Qwen 1280x736 input
contract: the old worker image rejects that keyframe geometry.

The canonical service entry is `ltx25` in `deploy/salad/services.json`. The manifest owns the
container group, queue, RTX 5090 profile, priority, replica ceiling and runtime environment.

Operate it with:

```powershell
.\scripts\salad\manage_salad_worker.ps1 -Service ltx25 -Action Status
.\scripts\salad\manage_salad_worker.ps1 -Service ltx25 -Action Prepare
.\scripts\salad\manage_salad_worker.ps1 -Service ltx25 -Action Start
.\scripts\salad\manage_salad_worker.ps1 -Service ltx25 -Action Stop
```

Queue autoscaling uses `min_replicas=0`; a started service does not imply an idle GPU replica.

## Reliability

Application job identity is independent from the Salad transport job ID. Phase 8 can therefore resume
or resubmit transport while preserving logical idempotency. Durable R2 output is checked before new
transport work when possible.

Paid validation uses dedicated scripts under `scripts/smoke/`; historical run IDs, image digests and
benchmark hashes stay in Git history.
