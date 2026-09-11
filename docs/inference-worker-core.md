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
compatible. `inputs` is now optional, allowing prompt-only inference jobs such as reference-image or
speech generation without inventing dummy R2 objects. Existing LTX requests remain unchanged.

The current response still represents one primary artifact. If a future task needs multiple persisted
artifacts, that should be introduced as an explicit new schema version rather than silently changing
schema v1.

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

The current LTX container continues to read `GPU_WORKER_*` and `GPU_WORKER_RUNTIME`; its runtime
adapts those legacy values into `InferenceWorkerSettings`. This compatibility layer is temporary and
lets the proven Phase 8 deployment keep working while model-specific containers are separated.

## Backward compatibility

`ai_video_factory.gpu` is now a compatibility facade for infrastructure primitives:

```text
GPUJobRequest  -> InferenceJobRequest
GPUJobResponse -> InferenceJobResponse
GPUWorker      -> InferenceWorker
```

Storage, repositories, errors, ports and the FastAPI app are also re-exported from the inference
core. LTX-specific code remains under `ai_video_factory.gpu` until the dedicated LTX worker migration.

The physical Postgres table remains `gpu.jobs` during this migration. Its name is an implementation
detail, not a public contract; renaming it is intentionally deferred to avoid unnecessary production
state migration while the worker architecture is changing.

## Model-specific container rule

Future Salad images must import the shared core and package only the dependencies needed by their
model. The intended direction is:

```text
docker/workers/ideogram4
docker/workers/breeze-tts2
docker/workers/whisper
docker/workers/keyframe
docker/workers/ltx25
```

Each service will have its own Salad queue and image lifecycle. Changing one model therefore does not
require rebuilding or pushing the others.
