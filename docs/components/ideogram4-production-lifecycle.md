# Ideogram 4 production lifecycle

This document describes the production contract for Ideogram 4 on SaladCloud. It applies to both Phase 4 reference assets and Phase 6 keyframes.

## Invariants

- Inference stays inside the open-weight Ideogram worker on SaladCloud. No hosted image-inference API is used.
- The Ideogram service uses one RTX 4090 at most: `min_replicas=0`, `max_replicas=1` in the normal manifest.
- Controlled runs preflight R2 before allocating a GPU.
- Prewarm requires an empty queue and a fully stopped group (`replicas=0`, `pending_change=false`).
- A controlled batch temporarily holds `min_replicas=1` only after exactly one instance reports `started=true` and `ready=true`.
- Every controlled run finishes through `Stop -> queue cleanup -> Status`. The normal manifest remains scale-to-zero.

## Startup state machine

The intended startup sequence is:

1. HTTP health process starts without accepting queue work.
2. Gated model download runs under the byte-progress/throughput watchdog.
3. The model `.ready` marker is validated.
4. Runtime imports begin.
5. CUDA/sampler runtime validation completes.
6. Pipeline configuration is built.
7. `Ideogram4Pipeline.from_pretrained()` starts.
8. Pipeline weights/runtime finish loading.
9. CUDA synchronization completes.
10. Pipeline reports ready.
11. `/ready` validates the resident runtime and records `worker_ready`.
12. Only then does the entrypoint start the Salad queue transport.

The backend writes the current stage atomically to `IDEOGRAM_BOOTSTRAP_STATUS_PATH` and emits `IDEOGRAM_BOOTSTRAP_STAGE` log lines. The external bootstrap watchdog emits `IDEOGRAM_BOOTSTRAP_PROGRESS` periodically, so a long `from_pretrained()` no longer produces an unobservable 10-20 minute gap.

Current runtime stages are:

- `validating_model_files`
- `model_files_validated`
- `importing_runtime`
- `runtime_imported`
- `validating_runtime`
- `runtime_validated`
- `building_pipeline_config`
- `pipeline_config_built`
- `from_pretrained`
- `pipeline_loaded`
- `cuda_synchronize`
- `cuda_synchronized`
- `pipeline_ready`
- `worker_ready`

## Startup timeouts and reallocation policy

Observed healthy nodes have completed the model download in roughly 150-165 seconds and the post-download runtime bootstrap in roughly six minutes. The runtime watchdog therefore uses deliberately wider finite limits:

- stage-stall timeout: 720 seconds;
- post-download hard bootstrap timeout: 900 seconds;
- watchdog poll interval: 15 seconds.

A stage that remains unchanged for 12 minutes or a post-download bootstrap that exceeds 15 minutes requests IMDS reallocation and terminates the failing bootstrap path. The public prewarm controller retains a 900-second `running/not-ready` outer guard so a dead or broken watchdog cannot leave a GPU running indefinitely.

The controller also tracks real Salad `machine_id` changes across the whole prewarm. Ideogram permits at most two node changes in one prewarm sequence. This is a global circuit breaker: changes caused by the worker IMDS watchdog and changes caused by the public controller consume the same observed node-change budget. Per-stage reallocation counters still exist, but they cannot combine into unbounded node churn.

## Queue lifecycle and abandoned work

No application job is submitted before prewarm succeeds. Pending inference timeouts are cancelled by transport ID.

A RUNNING timeout cannot be assumed cancellable because the job has already been dispatched. Controlled Ideogram runners therefore stop the container group first and then call `cleanup_salad_queue.ps1`. Cleanup:

- requires the group to be fully stopped at zero replicas;
- enumerates active queue jobs and logs both transport and application job IDs;
- cancels abandoned PENDING jobs;
- waits a finite 180 seconds for already-dispatched RUNNING jobs to become terminal;
- fails explicitly if an active/ghost job remains;
- never invokes queue `Repair`.

A failed cleanup is an operator-visible error, but the nested `finally` still executes `Status`.

## Safety policy

One worker application job performs exactly one Ideogram pipeline call. The worker never spends two or three additional inference attempts behind the same application job ID. A gray safety placeholder becomes the non-retryable worker result:

`Ideogram 4 safety filter blocked generated image`

The provider owns deterministic recovery. For both Phase 4 and Phase 6 it plans up to three independently fingerprinted application jobs:

1. `canonical` - the exact validated structured caption;
2. `safe_simplified` - narrative wording is reduced to essential first-sentence descriptions and style wording is neutralized while preserving photo-vs-art mode and visual invariants;
3. `safe_minimal_art` - a more structural minimal production-reference caption using clean concept art, while preserving composition/elements rather than inventing narrative content.

Each variant has its own deterministic application job ID, seed, R2 output cache entry, queue metadata and logs. The provider logs `prompt_variant`, application job ID, transport job ID and rejection reason.

## Safety rejection cache

A confirmed safety rejection is written to:

`jobs/<application_job_id>/rejections/safety.json`

The object metadata binds the rejection to the application job ID and exact request SHA-256. Before queue submission the provider checks both the normal output cache and the safety-rejection cache. A matching rejection skips the queue submission entirely.

The migration registry also contains the confirmed pre-cache safety rejects:

- `ideogram-reference-6927a43b3213ae83dbf86a52c8722525`
- `ideogram-reference-e6a0d2f5296bab78dce70cfd30460fa6`

The second entry is additionally bound to its known request SHA-256. These jobs must not be paid again.

## Phase 4 and Phase 6 controlled flow

Both phases use the same lifecycle:

`R2 preflight -> optimized prewarm -> exactly one ready replica -> temporary warm hold -> full batch -> Stop -> bounded queue cleanup -> Status`

The warm hold prevents Salad from destroying the resident worker during a short gap between deterministic prompt variants. `Stop` restores the scale-to-zero state after the batch.

## Operator recovery

Before a new controlled Ideogram run, verify all of the following:

- repository revision is the intended deployed revision;
- R2 preflight succeeds;
- queue is empty;
- group status is `stopped`;
- `replicas=0`;
- `pending_change=false`;
- remote autoscaler has `min_replicas=0`, `max_replicas=1`.

If prewarm fails, do not immediately rerun or call `Repair`. First execute `Stop`, inspect `Status`, and confirm zero replicas. Run queue cleanup only after the group is stopped. Use `Repair` only with direct evidence of a broken queue attachment.

If a runtime bootstrap stalls, use `IDEOGRAM_BOOTSTRAP_STAGE` and `IDEOGRAM_BOOTSTRAP_PROGRESS` to identify the exact stage and node. The bounded watchdog/controller policy should reallocate or abort without indefinite GPU burn.

If generation safety-rejects a variant, do not edit the rejection object or replay the same application job. The next deterministic provider variant should be used. If all variants are rejected, change the canonical visual plan meaningfully rather than deleting the rejection cache.

## Deployment rule

The runtime-bootstrap changes require a new worker image (`ideogram4-nf4-quality48-v4`). Build and CI must be green before deployment. Perform exactly one Ideogram `Prepare` for that image, then return the service to scale-to-zero. Do not Prepare Breeze, Whisper or LTX as part of this change.
