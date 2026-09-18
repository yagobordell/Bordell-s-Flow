# FLUX.2 Klein 4B safety fallback

## Purpose

Ideogram 4 remains the primary provider for Phase 4 reference images and Phase 6 keyframes.
FLUX.2 Klein 4B is a fallback only when Ideogram returns confirmed terminal safety rejection for all
executable deterministic variants of one request.

Infrastructure errors, network failures, timeouts, OOMs, queue failures, capacity problems and model
bootstrap failures are **not** fallback triggers.

## Model and worker

Validated production configuration:

- Model: `black-forest-labs/FLUX.2 Klein 4B`.
- Salad service: `flux2_klein`.
- Queue: `ai-video-factory-flux2-klein-jobs`.
- GPU: RTX 4090 (24 GB).
- Autoscaler: min replicas 0, max replicas 1.
- Image tag: `flux2-klein-4b-bf16-v1`.
- Validated image digest:
  `sha256:de8caee7704e92c4f519b66cb8ed32c11069488ae30557d78b4fe3cfe1d1c2e5`.
- Transformer and T5 encoder use bitsandbytes NF4 4-bit quantization.
- Generation identity is deterministic: application job ID, seed, prompt rendering, dimensions and
  four inference steps are fixed by the application request.

The Hugging Face repository is gated. The account behind `HF_TOKEN` must have access before the first
worker bootstrap.

## Selective model bootstrap

The worker downloads only the Diffusers runtime layout used by the production loader:

```text
model_index.json
scheduler/
text_encoder/
text_encoder_2/
tokenizer/
tokenizer_2/
transformer/
vae/
```

Top-level monolithic checkpoints that are not used by `FluxPipeline.from_pretrained(...)` are not
downloaded. The validated selective snapshot is about 33.7 GB.

The image explicitly installs `sentencepiece` and `protobuf`, which are required by the T5 tokenizer
path. The Docker build smoke-check imports both dependencies so a missing tokenizer runtime dependency
fails before Salad GPU allocation.

The bootstrap writes `.ready` only after the selected Diffusers snapshot is complete. Production
readiness remains false while the model is still downloading or the pipeline is still being prepared.

## Phase 4 planning

`audit_phase4_reference_cache.py` performs a read-only R2 plan before GPU allocation:

- `hit`: an Ideogram or FLUX artifact is already cached; no queue work.
- `miss`: at least one executable Ideogram variant is still eligible; Ideogram remains primary.
- `safety_blocked`: all executable Ideogram variants are terminal safety rejections and no FLUX
  artifact exists; skip Ideogram and use FLUX.
- `invalid`: cached metadata or content integrity is invalid; stop before GPU allocation.

The validated Phase 4 Ideogram safety variants are:

```text
canonical
safe_simplified
safe_minimal_art
```

`run_phase4_assets_controlled.ps1` consumes the plan. A safety-only batch does not prewarm Ideogram.
If FLUX is required, it performs deterministic FLUX prewarm before any fallback queue submission.

## Deterministic prewarm

Scale-to-zero alone is not sufficient for this workload: the production path explicitly requests one
FLUX replica before queue submission.

Prewarm:

1. verifies the group/queue configuration;
2. exhaustively checks that there are no enumerable `pending` or `running` FLUX jobs;
3. requests exactly one replica;
4. starts the group;
5. waits for one instance with `started=True` and `ready=True`;
6. temporarily holds one ready replica while fallback queue work runs.

Only after readiness is confirmed does the Phase 4 or Phase 6 workflow submit FLUX inference work.
This keeps cold bootstrap outside the transport job's pending timeout.

Do not use `Repair` as part of the normal lifecycle.

## Phase 6 behavior

Phase 6 keeps Ideogram as the primary keyframe generator. If a keyframe reaches confirmed terminal
Ideogram safety rejection, the controlled workflow uses the same FLUX safety-only policy and the same
provenance contract.

The controlled wrapper restores both services after execution. Cached successful output must be
replayed rather than reinferred.

## Cache and provenance

FLUX output uses its own deterministic application job IDs (`flux-reference-*` / `flux-keyframe-*`)
and R2 objects. Generated metadata records:

- `provider=flux2_klein`
- `model=black-forest-labs/FLUX.2 Klein 4B`
- `fallback_from=ideogram4`
- `fallback_reason=safety_rejection`
- application job ID
- request SHA-256
- prompt SHA-256 where applicable
- replay state

This keeps fallback outputs auditable and prevents repeated paid inference after a successful
generation.

## Scale-to-zero restore

The controlled wrappers restore the manifest queue autoscaler and require the final invariant:

```text
status=stopped
replicas=0
pending_change=False
```

Salad can keep a stopped group at a nonzero desired replica count. Restore therefore treats autoscaler
restoration and desired-replica normalization as separate control-plane operations when necessary.
The separate `replicas=0` merge patch matches the behavior already proven by the Prepare workflow.

## Stale queue summaries

Salad queue summary metadata can remain nonzero after all enumerable jobs are terminal.
`cleanup_salad_queue.ps1` therefore uses exhaustive job enumeration as the source of truth for active
work when the summary is nonzero:

- `pending` and `running` are active;
- terminal jobs are not active;
- a stale nonzero summary is tolerated only after exhaustive pagination completes with no active jobs;
- incomplete enumeration remains a hard failure.

This prevents a stale control-plane counter from forcing queue recreation or unsafe `Repair` actions.

## Deployment

Use the manifest-authoritative Prepare wrapper whenever the FLUX image or configuration changes:

```powershell
.\scripts\prepare_salad_worker_manifest.ps1 `
  -Service flux2_klein `
  -PrepareTimeoutMinutes 120 `
  -NonInteractive
```

Do not use `-SkipBuild` when deploying a new FLUX image tag. Prepare must finish with the group stopped
at zero replicas before production generation begins.

The full Phase 4 production validation and zero-GPU replay evidence are recorded in
[`phase4-closure.md`](phase4-closure.md).


## Runtime identity

- Revision: `e7b7dc27f91deacad38e78976d1f2b499d76a294`.
- Dtype: BF16.
- Pipeline: `Flux2KleinPipeline`.
- Defaults: 4 inference steps, guidance 1.0, deterministic seeded CUDA generator.
- Download: pinned Diffusers components only, Xet high-performance mode, bounded watchdog with
  slow-download detection and Salad reallocation.
- Cache identity includes model, revision, generation profile, dimensions and prompt hash, so
  FLUX.1 fallback artifacts are not replayed as FLUX.2 outputs.
