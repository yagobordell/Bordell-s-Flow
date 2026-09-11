# Dedicated LTX-2.5 worker

LTX-2.5 is the first model runtime migrated from a phase-specific GPU image to the shared inference-worker architecture.

The canonical production boundary is now:

```text
local orchestrator
    |
    v
Salad queue: ai-video-factory-ltx25-jobs
    |
    v
docker/workers/ltx25
    |
    v
ai_video_factory.workers.ltx25.runtime
    |
    +--> shared ai_video_factory.inference worker core
    |
    +--> video.ltx25.generate only
    |
    +--> LTX-2.5 direct Python/PyTorch runtime
    |
    +--> R2 artifacts + Postgres leases/state
```

## Ownership

The shared `ai_video_factory.inference` package owns transport-independent execution concerns:

- immutable job fingerprints;
- object download/upload and SHA-256 validation;
- Postgres claims, leases, retries and replay;
- `/health`, `/ready` and `/jobs`;
- model-agnostic task registration.

The dedicated `ai_video_factory.workers.ltx25` package owns only LTX-specific behavior:

- the `video.ltx25.generate` task;
- LTX parameter and shape validation;
- model file layout;
- direct LTX pipeline construction;
- FP8-cast + CPU-offload runtime configuration;
- deterministic LTX application job IDs.

Legacy imports under `ai_video_factory.gpu.ltx_video` and `ai_video_factory.gpu.ltx_jobs` remain compatibility facades so existing Phase 8 manifests, tests and scripts continue to work while callers migrate.

## Container

The canonical Dockerfile is:

```text
docker/workers/ltx25/Dockerfile
```

The old `docker/phase8-worker` image definition has been removed. The new image copies only the Python package sources required by the worker rather than the whole repository. Remotion, tests, docs, local orchestration scripts and unrelated future model runtimes are not bundled into the LTX image.

The LTX source runtime remains pinned to the already validated git commit:

```text
a95ab856bf29407b6b066ede0abe1846050db56c
```

Model weights are bootstrapped after the HTTP health endpoint starts. The model repository revision is controlled separately through:

```text
LTX_MODEL_REVISION
```

This permits pinning an exact validated Hugging Face revision in deployment configuration without coupling it to the container runtime source commit.

## Salad service

Model-specific Salad configuration lives in:

```text
deploy/salad/services.json
```

The LTX service owns the dedicated queue:

```text
ai-video-factory-ltx25-jobs
```

The existing validated Salad container group is reused as the LTX deployment slot during this migration. The service manifest binds that slot to the LTX image and queue; future model workers will receive their own service entries, container groups and queues.

The generic operator command is:

```powershell
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Status
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Prepare
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Start
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Stop
```

`Prepare` builds and pushes only the LTX image, resolves the mutable tag to an immutable digest, ensures the dedicated queue exists, injects runtime secrets from the process environment or secure prompt, and patches the configured Salad deployment slot while it is stopped.

`Start` and `Stop` operate on that service only. With `min_replicas=0`, starting the container group enables queue-driven autoscaling without forcing an idle GPU replica.

The old `manage_phase8_worker.ps1`, `start_phase8_autoscaled.ps1`, and `status_phase8_instances.ps1` commands remain compatibility wrappers around the LTX service manager.

## Orchestrator queue selection

`run_phase8_videos.py` now resolves its queue in this order:

```text
SALAD_LTX25_QUEUE_NAME
SALAD_QUEUE_NAME              # legacy fallback
ai-video-factory-ltx25-jobs   # canonical default
```

The dedicated setting should be used for new environments:

```text
SALAD_LTX25_QUEUE_NAME=ai-video-factory-ltx25-jobs
```

Existing deterministic application job IDs deliberately retain the `phase8-shot-*` prefix. They are persisted identity, not deployment naming, and changing them would invalidate successful replay/resume state for no functional benefit.

## Template for later workers

Ideogram, Breeze TTS, Whisper and the future keyframe model should follow the same boundary:

```text
workers/<model>/
    model.py
    settings.py
    runtime.py

docker/workers/<model>/
    Dockerfile
    entrypoint.sh
    model bootstrap if required

deploy/salad/services.json
    one service entry
    one queue
    one model-specific image
```

They should import the shared inference core rather than copying LTX infrastructure or depending on the `gpu` compatibility namespace.
