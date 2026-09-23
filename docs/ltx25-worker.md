# Dedicated LTX-2.5 worker

LTX-2.5 is the first model runtime migrated from a phase-specific GPU image to the shared inference-worker architecture. The dedicated deployment was revalidated end to end on real SaladCloud infrastructure on 2026-09-15.

The canonical production boundary is now:

```text
local orchestrator
    |
    v
Salad queue: ai-video-factory-ltx25-jobs-v2
    |
    v
docker/workers/ltx25
    |
    v
ai_video_factory.workers.ltx25.runtime
    |
    +--> shared ai_video_factory.inference worker core
    |
    +--> video.ltx25.generate
    +--> video.ltx25.audio_to_video
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

The dedicated `ai_video_factory.workers.ltx25` package owns only LTX-specific behavior.
The A2V/avatar-specific contract and smoke process are documented in
[`ltx25-a2v.md`](ltx25-a2v.md).


- the `video.ltx25.generate` and `video.ltx25.audio_to_video` tasks;
- LTX parameter and shape validation;
- model file layout;
- direct LTX pipeline construction;
- FP8-cast + CPU-offload runtime configuration;
- deterministic LTX application job IDs.


## Container

The canonical Dockerfile is:

```text
docker/workers/ltx25/Dockerfile
```

The old `docker/phase8-worker` image definition has been removed. The new image copies only the Python package sources required by the worker rather than the whole repository. Remotion, tests, docs, local orchestration scripts and unrelated model runtimes are not bundled into the LTX image.

The LTX source runtime remains pinned to the validated git commit:

```text
a95ab856bf29407b6b066ede0abe1846050db56c
```

Model weights are bootstrapped after the HTTP health endpoint starts. The model repository revision is controlled separately through `LTX_MODEL_REVISION`.

## Salad service

Model-specific Salad configuration lives in `deploy/salad/services.json`.

The current dedicated service is:

> 2026-09-20 routing migration: production moved from
> `ai-video-factory-ltx25-worker` / `ai-video-factory-ltx25-jobs` to the fresh
> `-v2` group/queue pair after repeated real runs proved that ready RTX 5090 workers
> were not receiving queued jobs. The old queue also reported a persistently stale
> historical queue length. Image, model, GPU class and inference settings are unchanged.

> The 2026-09-15 validation used medium priority. Production was raised to high priority on
> 2026-09-20 after repeated real runs could not obtain any RTX 5090 placement at medium priority.
> The GPU class and inference runtime remain unchanged.

```text
service:      ltx25
group:        ai-video-factory-ltx25-worker-v2
queue:        ai-video-factory-ltx25-jobs-v2
GPU:          RTX 5090 (32 GB)
CPU:          8
memory:       61440 MiB
shared memory:8192 MiB
storage:      171798691840 bytes
priority:     high
autoscaler:   min=0, max=4
```

The 2026-09-15 real validation used container-group version 5 and immutable image:

```text
docker.io/yagobordell/ai-video-factory@sha256:598d743b82f75e531cf29c521530a5b9d8d606a6d98a4e7b9fd737811c384a02
```

The generic operator commands are:

```powershell
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Status
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Prepare
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Start
.\scripts\manage_salad_worker.ps1 -Service ltx25 -Action Stop
```

`Prepare` builds and pushes only the LTX image, resolves the mutable tag to an immutable digest, ensures the dedicated queue exists, injects runtime secrets, and patches the configured Salad deployment slot while it is stopped. It leaves the group stopped with zero replicas.

`Start` and `Stop` operate on that service only. With `min_replicas=0`, enabling the group permits queue-driven autoscaling without forcing an idle GPU replica.

The old `manage_phase8_worker.ps1`, `start_phase8_autoscaled.ps1`, and `status_phase8_instances.ps1` commands remain compatibility wrappers around the LTX service manager.

## Model bootstrap and watchdogs

`docker/workers/ltx25/download_models.sh` materializes the seven production checkpoints sequentially under `/workspace/models/ltx-2.5`:

```text
diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
diffusion_models/ltx-2.5-22b-dev-transformer-bf16.safetensors
text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
vae/ltx-2.5-video-vae-bf16.safetensors
vae/ltx-2.5-audio-vae-bf16.safetensors
latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors
loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors
```

The downloader emits `MODEL_DOWNLOAD_PROGRESS` every 30 seconds using observed local download bytes. Any byte growth resets the idle timer. Ten minutes with no byte growth produces `MODEL_DOWNLOAD_STALLED` and terminates that download attempt.

The successful real validation showed that the large cold start is slow but healthy: all five files completed, the worker reached `ready=true`, and no stall event occurred.

Salad instance storage is ephemeral, so a new allocation can repeat the download even after a previous successful run. Persistent/external model caching is an optimization candidate, not part of the current correctness baseline.

## Readiness and queue transport

The lifecycle is:

```text
container starts
  -> /health = 200
  -> checkpoint bootstrap
  -> resident pipeline preparation
  -> /ready = 200
  -> Salad Job Queue transport starts
```

During a real job `/ready` may temporarily become false while the worker is busy. In the successful 2026-09-15 smoke it returned to true immediately after the job completed.

Salad's `queue.container_groups` listing is not used as a hard readiness gate. The successful smoke observed `attached=False` while the worker nevertheless received and completed the queued job. Runtime delivery is authoritative.

## Cost-guarded real smoke

The canonical expensive validation command is:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_ltx25_protected_smoke.ps1 `
  -SkipLocalBuild
```

The wrapper uses:

```text
healthy bootstrap deadline:          45 minutes
running/not-ready reallocation:      60 minutes
no-progress model-download timeout:  600 seconds
```

It always executes the normal LTX `Stop` path in `finally`, so a success or failure returns the group to the stopped/zero-replica state unless the cleanup itself reports an error.

The 2026-09-15 smoke completed:

```text
application_job_id: phase8-shot-001-730e00d82f95
salad_job_id:       e06633cb-814e-4e77-99c3-a2b5b0f2f9fd
status:             succeeded
resolution:         768x1280
fps:                24
num_frames:         25
replayed:           false
output sha256:      ff82d028b04bb5cf91a7198bf75f2e7cf6e585e1cbf25ca8679b376956dc655a
```

The final cleanup reported `stopped / replicas=0 / pending=False`.

Full evidence: .

## Orchestrator queue selection

`run_phase8_videos.py` resolves its queue in this order:

```text
SALAD_LTX25_QUEUE_NAME
SALAD_QUEUE_NAME              # legacy fallback
ai-video-factory-ltx25-jobs-v2 # canonical default
```

The dedicated setting should be used for new environments:

```text
SALAD_LTX25_QUEUE_NAME=ai-video-factory-ltx25-jobs-v2
```

Existing deterministic application job IDs deliberately retain the `phase8-shot-*` prefix. They are persisted identity, not deployment naming, and changing them would invalidate successful replay/resume state for no functional benefit.

## Template for later workers

Other model workers follow the same boundary:

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

They import the shared inference core rather than copying LTX infrastructure or depending on the `gpu` compatibility namespace.
