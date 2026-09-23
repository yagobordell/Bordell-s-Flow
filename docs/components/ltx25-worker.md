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

Accepted clips are subsequently upscaled by Real-ESRGAN to 2560x1440.

## Container and bootstrap

The image lives under `docker/workers/ltx25` and packages the pinned LTX runtime plus the shared
inference worker.

Required model checkpoints are downloaded under the configured model root. LTX now reuses
`ai_video_factory.workers.download_watchdog` rather than maintaining a separate Bash watchdog.
Existing non-empty checkpoint files take the fast path and are not downloaded again.

## Deployment

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
