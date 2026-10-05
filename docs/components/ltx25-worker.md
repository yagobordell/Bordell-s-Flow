# LTX 2.5 worker

LTX 2.5 is the production video-generation worker. It handles normal image-to-video shots and exposes
the audio-to-video capability used by avatar-oriented flows.

## Boundary

Normal Phase 8 generation receives a storyboard keyframe, motion prompt and timing-derived generation
parameters, then writes one silent MP4 artifact per shot.

Production `run` video geometry is:

```text
1920x1080
24 fps
H.264 MP4
```

Qwen text-to-image keyframes are 1280x736 rather than exact 16:9. For I2V, the worker
preserves the entire source frame using proportional fit and narrow edge-extended side margins,
scales it to the 1920x1080 delivery frame, then pads only the model grid to 1920x1088.
That eight-pixel internal height margin is removed after decoding. The public I2V MP4 is
1920x1080 and remains silent; A2V carries its generated speech stream until final composition.

The current `run` path does not use a video upscaler after LTX; LTX delivers the final
1920x1080 clip geometry directly.

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
The image remains intentionally weight-light: checkpoints are not baked into the OCI image.
`download_models.sh` emits `LTX25_MODEL_BOOTSTRAP_METRIC` with cache-hit state and elapsed
bootstrap time so paid runtime can be separated from model transfer/verification.

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
I2V and A2V must use independent cold-start/runtime calibration because they are separate
container groups even though they currently share the same worker image. Both production LTX
groups are capped at two RTX 5090 replicas and default to one queued inference job per replica,
so two pending I2V clips or two pending A2V clips request two independent GPUs and execute
concurrently even when their durations differ. The controller is the only production authority
allowed to resize or stop those groups; workers never stop Salad directly.
Controlled paid smokes allocate one replica explicitly and stop it in `finally`.
See [isolated I2V validation](../operations/ltx25-i2v-isolated-smoke.md).

## Reliability

Application job identity is independent from the Salad transport job ID. Phase 8 can therefore resume
or resubmit transport while preserving logical idempotency. Durable R2 output is checked before new
transport work when possible.

Healthy jobs reuse a resident pipeline within one replica: I2V loads during worker preparation,
while A2V loads lazily on the first generation for a profile. A retryable GPU failure
(`oom`, `cuda_device` or `cudnn_sdpa`) invalidates the affected resident pipeline before the
shared retry cooldown. The retry then rebuilds from the already-downloaded local checkpoints instead
of forcing another Hugging Face bootstrap or Salad cold start. Non-GPU failures do not discard a
healthy resident pipeline.

I2V emits pipeline-load and inference timing metrics; A2V already records model-load, inference,
encode/mux and pipeline-reuse metrics. Generic worker logs record task start/end and durable artifact
publication. These boundaries are the source of truth for the later RTX 5090 lifecycle benchmark.

Paid validation uses dedicated scripts under `scripts/smoke/`; historical run IDs, image digests and
benchmark hashes stay in Git history.
