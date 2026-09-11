# Shared inference worker core

The remote execution boundary is model-neutral. New Salad workers should build on
`ai_video_factory.inference` and register only the task runners required by their model image.

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

The shared core owns:

- deterministic request fingerprints;
- application job identity independent from Salad transport IDs;
- Postgres claims, leases, heartbeat renewal and replay;
- R2/local object download and integrity validation;
- deterministic output reconciliation after upload/commit crashes;
- `/health`, `/ready` and `/jobs` HTTP behavior;
- task registration and runtime preparation hooks.

A model worker owns only its dependencies, model bootstrap and one or more `TaskRunner`
implementations. Model-specific imports must not be added to `ai_video_factory.inference`.

## Contracts

`InferenceJobRequest` keeps the existing schema-v1 wire shape so the validated LTX deployment remains
compatible. `inputs` is optional, allowing prompt-only inference jobs such as reference-image or
speech generation without inventing dummy R2 objects. Existing LTX requests remain unchanged.

The current response represents one primary artifact. Whisper uses that artifact for `words.json`,
Breeze uses it for the narration WAV and LTX uses it for the generated MP4. If a future task needs
multiple persisted artifacts, that should be introduced as an explicit new schema version rather than
silently changing schema v1.

## Configuration

New workers use the neutral infrastructure variables:

```text
INFERENCE_WORKER_MODE
INFERENCE_WORKER_TEMP_DIR
INFERENCE_WORKER_LEASE_SECONDS
INFERENCE_WORKER_HEARTBEAT_SECONDS
INFERENCE_WORKER_MAX_DB_CONNECTIONS
INFERENCE_LOCAL_OBJECT_ROOT
```

R2 and Postgres credentials retain their shared names (`R2_*`, `POSTGRES_DSN`).

Model-specific settings live under each worker namespace. For example, LTX reads `LTX_*`, Breeze
reads `BREEZE_*` and Whisper reads `WHISPER_*`; those settings do not leak into the shared inference
package.

## Backward compatibility

`ai_video_factory.gpu` remains a compatibility facade for infrastructure primitives that existed
before the neutral inference namespace:

```text
GPUJobRequest  -> InferenceJobRequest
GPUJobResponse -> InferenceJobResponse
GPUWorker      -> InferenceWorker
```

Storage, repositories, errors, ports and the FastAPI app are also re-exported from the inference core
where older Phase 7/8 code still imports them. The canonical LTX runtime is
`ai_video_factory.workers.ltx25`, narration is `ai_video_factory.workers.breeze_tts2`, and
transcription is `ai_video_factory.workers.whisper`.

The physical Postgres table remains `gpu.jobs` during this migration. Its name is an implementation
detail, not a public contract; renaming it is intentionally deferred to avoid unnecessary production
state migration while the worker architecture is changing.

## Model-specific container rule

Salad images import the shared core and package only the dependencies needed by their model. The
current/target layout is:

```text
docker/workers/ideogram4       # planned; Phase 4 references + Phase 6 keyframes
docker/workers/breeze-tts2     # implemented
docker/workers/whisper         # implemented
docker/workers/ltx25           # implemented
```

Ideogram 4 Quality is the selected keyframe model as well as the Phase 4 reference-image model. There
is intentionally no second `keyframe` image: both workloads share one model-specific queue and
container image, while Salad creates multiple replicas of that group to run independent image jobs in
parallel.

Each model family has its own Salad queue and image lifecycle. Changing one model therefore does not
require rebuilding or pushing the others. A worker remains serialized within one GPU; horizontal
parallelism comes from queue-autoscaled container replicas.

On the client side, `InferenceJobExecutor` provides the reusable submit/poll/verify/download loop for
simple one-artifact inference providers. Whisper and Breeze use this path without coupling their
domain contracts to Salad transport details.

Hardware profiles and replica ceilings are documented in `docs/salad-gpu-profiles.md`.
