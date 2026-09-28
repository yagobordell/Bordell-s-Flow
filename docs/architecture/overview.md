# Architecture overview

The active planning pipeline is B1.1 → B1.2 → B2. It produces a source-preserving
visual plan with string beat IDs and avatar/image/video strategies. Each run
selects a local PNG avatar, snapshots it and records the binding in the
application-owned visual plan without modifying B2's bot contracts. The previous
scene/shot-based planning bots and end-to-end production runner have been retired.
GPU stage clients remain usable independently, but there is no supported
end-to-end B2-to-final-video path until their contracts are migrated.

## Application control plane

Postgres `gpu.jobs` is the authoritative queue. Providers submit deterministic
`InferenceJobRequest` objects; dedicated workers on Salad claim jobs with leases,
read their inputs from R2, execute inference and publish durable outputs.
R2 manifests and request fingerprints provide replay and recovery.

## Salad's role

Salad hosts the compute services defined in `deploy/salad/services.json`.
Breeze TTS 2, Fish Speech, Whisper, Qwen Image 2.1, Ideogram 4 (optional),
LTX 2.5 and Real-ESRGAN retain their worker runtimes and client integrations.
The global Capacity Controller reconciles project capacity against Postgres
demand. Model workers share inference contracts, health checks, Postgres
claims/heartbeats, storage integrity and recovery. Salad is not the queue
authority.

Service management, controlled per-stage GPU runners and smoke tests remain
independent of the retired planning bots. The B runner itself calls OpenAI
and does not allocate Salad GPU capacity.

## Execution

Use `scripts/pipeline/run_b_pipeline.py` for the active planning bots.
Use the service-specific wrappers and smoke tests for GPU validation. There is
no general video-production entrypoint until the downstream migration is tested.
See [B pipeline](../components/b-pipeline.md) and
[production-runner transition](../operations/production-runner.md).
