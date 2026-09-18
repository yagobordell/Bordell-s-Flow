# FLUX.2 Klein 4B safety fallback

## Purpose

Ideogram 4 remains the primary provider for Phase 4 reference images and Phase 6 keyframes.
FLUX.2 Klein 4B is used only after confirmed terminal Ideogram safety rejection for all executable
deterministic variants of the request.

Infrastructure errors, network failures, timeouts, OOMs, queue failures, capacity problems and model
bootstrap failures are not fallback triggers.

## Production identity

- Model: `black-forest-labs/FLUX.2-klein-4B`.
- Revision: `e7b7dc27f91deacad38e78976d1f2b499d76a294`.
- Pipeline: `Flux2KleinPipeline`.
- Dtype: BF16.
- Generation profile: `flux2-klein-4b-bf16-v1`.
- Defaults: 4 inference steps, guidance 1.0, deterministic seeded CUDA generator.
- Salad service: `flux2_klein`.
- Container group: `ai-video-factory-flux2-klein-worker`.
- Queue: `ai-video-factory-flux2-klein-jobs`.
- GPU: RTX 4090 (24 GB).
- Resources: 8 CPU, 32768 MiB RAM, 8192 MiB shared memory, 96 GiB ephemeral storage.
- Autoscaler: min replicas 0, max replicas 1.
- Validated immutable image:
  `docker.io/yagobordell/ai-video-factory@sha256:789a065c072898493a416ed6483b1ac72e04c1a0968cfa615efe325683ea895a`.

The selected checkpoint is public; `HF_TOKEN` is optional. The worker does not use bitsandbytes or
CPU offload. BF16 is loaded directly on the GPU.

## Selective model bootstrap

The worker downloads only the Diffusers layout needed by `Flux2KleinPipeline.from_pretrained(...)`:

```text
model_index.json
scheduler/
text_encoder/
tokenizer/
transformer/
vae/
```

The snapshot is pinned to the exact model revision and the worker writes `.ready` only after the
snapshot is complete. Runtime readiness remains false until both the marker identity is valid and the
pipeline has been loaded onto CUDA.

The download is guarded by the shared watchdog with bounded stall/hard timeouts, throughput
monitoring and Salad reallocation on sustained slow download.

## Real Salad validation — 2026-09-18

A real scale-to-zero Salad smoke was executed against the immutable image above. The run exercised
Salad Queue -> FLUX.2 Klein worker -> R2 upload -> client R2 download -> PNG validation, then restored
the container group to zero replicas and cleaned the queue.

Observed model/runtime metrics:

- Selective snapshot size: 15,980,138,506 bytes (about 14.88 GiB).
- Snapshot completed: `2026-09-18T13:49:27.363705Z`.
- Bootstrap marker completed: `2026-09-18T13:49:31.351729Z`.
- CUDA pipeline load: 4.097 s.
- Resident CUDA memory after pipeline load:
  - allocated: 16,027,997,184 bytes (about 14.93 GiB);
  - reserved: 16,032,727,040 bytes (about 14.93 GiB).
- 1024x1024 inference: 3.405 s, 4 steps, guidance 1.0.
- Peak inference CUDA memory:
  - allocated: 18,602,007,552 bytes (about 17.32 GiB);
  - reserved: 21,019,754,496 bytes (about 19.58 GiB).
- End-to-end queued smoke elapsed time after prewarm: 12.996 s.
- Output: 1024x1024 PNG, 1,731,486 bytes (about 1.65 MiB).
- `replayed=false`, confirming real inference rather than cache replay.
- Provenance:
  - provider `flux2_klein`;
  - fallback source `ideogram4`;
  - fallback reason `safety_rejection`.
- Final Salad invariant: group `stopped`, `replicas=0`, `pending_change=False`; no active or
  queued fallback jobs remained.

The 24 GB RTX 4090 therefore has roughly 4.4 GiB of headroom versus peak reserved CUDA memory in this
representative 1024x1024 BF16 run. The BF16 profile is retained; the official FP8 checkpoint is not
needed for the current fallback workload.

The prewarm control script also emits `assignment_seconds`, `container_started_seconds`,
`image_pull_and_start_seconds`, `ready_seconds` and `bootstrap_after_start_seconds`. Historical
Salad log collection captures runtime/inference GPU metrics without starting another replica.

## Safety fallback routing

The fallback wrapper catches only terminal Ideogram safety rejection. Ordinary provider failures are
propagated instead of being silently rerouted to FLUX.2.

Phase 4 and Phase 6 both use `SaladFlux2KleinImageProvider` with distinct task names for references
and keyframes. Ideogram remains primary in both phases.

## Deterministic identity and cache invalidation

FLUX.2 output identity includes:

- generation profile;
- exact model ID and revision;
- purpose (reference/keyframe);
- requested dimensions;
- prompt SHA-256.

Application job IDs use `flux2-klein-reference-...` or `flux2-klein-keyframe-...`. This prevents
legacy FLUX.1 fallback artifacts from matching FLUX.2 requests while preserving normal replay of
successful FLUX.2 outputs.

Generated metadata records provider, model, model revision, fallback source/reason, application job
ID, request SHA-256 and replay state.

## Cold-start lifecycle

The controlled prewarm path:

1. verifies group/queue identity and scale-to-zero configuration;
2. exhaustively checks that no fallback jobs are active;
3. requests exactly one replica;
4. starts the group;
5. waits for one instance with `started=True` and `ready=True`;
6. holds that ready replica while fallback queue work runs.

The HTTP `/health` endpoint is available before model download. `/ready` remains false until the
pinned snapshot and CUDA pipeline are usable. Queue transport starts only after readiness succeeds.

The controlled smoke and Phase 4/6 wrappers restore the manifest autoscaler, normalize desired
replicas to zero, stop the group and clean the queue in `finally` paths.

## Deployment

Prepare a changed worker image/configuration with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
    .\scripts\prepare_salad_worker_manifest.ps1 \
    -Service flux2_klein \
    -NonInteractive
```

Run the protected end-to-end validation with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
    .\scripts\run_flux2_klein_smoke_controlled.ps1 \
    -NonInteractive
```

Historical GPU metrics for an already completed run can be queried without starting a GPU via
`scripts/collect_flux2_klein_salad_metrics.ps1`.

Do not use `Repair` as part of the normal lifecycle.
