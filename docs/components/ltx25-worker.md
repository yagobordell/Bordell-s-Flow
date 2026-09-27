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

`deploy/salad/services.json` defines separate Salad groups: `ltx25_i2v` handles only
`video.ltx25.generate`, and `ltx25` handles only avatar A2V jobs.
`LTX_WORKER_TASK_MODE` restricts registered task runners and Postgres polling.
Each group has independent RTX 5090 capacity, GPU instances and autoscaling.
The isolated v11 I2V smoke (`i2v-smoke-003`) produced one technically valid
2-second MP4 but showed unwanted camera push-in. The opt-in `static-camera`
profile uses camera-only negative CFG 1.25 on Stage 1 of the same resident
distilled I2V pipeline, retaining full first-frame strength 1.0 and Stage 2.
The real v12 comparison (`i2v-static-camera-001`) was technically successful,
with less but still clearly visible zoom; it is NOT a camera-lock guarantee.
The v12 linux/amd64 manifest is
`sha256:6aed710bedc75aea072a40108a20887bbd578e7520d39dfe4d961e3e745a2bf9`.
The tracked manifest intentionally retains the v11 tag until deployment is
separately approved; the tested Salad I2V group was pinned to v12 and stopped
after the controlled smoke. Do not enable the new capacity controller until
both groups are prepared with task-restricted images, including the A2V group.

Operate it with:

```powershell
.\scripts\salad\manage_salad_worker.ps1 -Service ltx25_i2v -Action Status
.\scripts\salad\manage_salad_worker.ps1 -Service ltx25_i2v -Action Prepare
.\scripts\salad\manage_salad_worker.ps1 -Service ltx25_i2v -Action Start
.\scripts\salad\manage_salad_worker.ps1 -Service ltx25_i2v -Action Stop
```

Production capacity is governed by the Postgres-backed predictive controller.
Controlled paid smokes allocate one replica explicitly and stop it in `finally`.
See [isolated I2V validation](../operations/ltx25-i2v-isolated-smoke.md).

## Reliability

Application job identity is independent from the Salad transport job ID. Phase 8 can therefore resume
or resubmit transport while preserving logical idempotency. Durable R2 output is checked before new
transport work when possible.

Paid validation uses dedicated scripts under `scripts/smoke/`; historical run IDs, image digests and
benchmark hashes stay in Git history.
