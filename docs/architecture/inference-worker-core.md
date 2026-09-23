# Shared inference worker core

`ai_video_factory.inference` is the model-neutral execution boundary used by Salad workers.

```text
Salad Job Queue
      |
      v
InferenceJobRequest
      |
      v
InferenceWorker
  |       |       |
  |       |       +--> TaskRunnerRegistry -> model adapter
  |       +----------> JobRepository -> Postgres leases/idempotency
  +------------------> ObjectStorage -> R2 artifacts
      |
      v
InferenceJobResponse
```

## Responsibilities

The shared core owns request fingerprints, application job identity, Postgres claims and leases,
R2/local object transfer, replay, output reconciliation, `/health`, `/ready`, `/jobs` and task
dispatch.

Model packages under `ai_video_factory.workers` own only model settings, bootstrap/runtime preparation
and task runners. Model-specific dependencies must not leak into the shared inference package.

## Contracts

Schema v1 represents one primary artifact per inference request. Prompt-only jobs may omit binary
inputs. A future multi-artifact response should use an explicit schema change rather than changing the
meaning of the current envelope.

Shared infrastructure configuration uses `INFERENCE_*`, `R2_*` and `POSTGRES_DSN`. Model settings
stay namespaced under their worker prefix.

The physical Postgres table remains `gpu.jobs`; its name is an implementation detail retained for
compatibility with deployed state.

## Worker boundary

Production and preserved model workers live under:

```text
src/ai_video_factory/workers/
docker/workers/
deploy/salad/services.json
```

The current manifest defines Whisper, Breeze TTS 2, Fish Speech, Ideogram 4, Qwen Image 2.1,
LTX 2.5 and Real-ESRGAN. Each service has its own queue and container group while sharing the same
inference core.

Workers process one model call at a time per GPU. Horizontal parallelism comes from Salad replicas,
not concurrent inference inside one worker process.

`InferenceJobExecutor` provides the reusable client-side submit/poll/verify/download path for simple
one-artifact providers.
