# FLUX.1-schnell safety fallback

## Purpose

Ideogram 4 remains the primary provider for Phase 4 reference images and Phase 6 keyframes. FLUX.1-schnell is a fallback only when Ideogram returns a confirmed terminal safety rejection. Infrastructure errors, timeouts, OOMs, queue failures, and bootstrap failures are not fallback triggers.

## Model and worker

- Model: `black-forest-labs/FLUX.1-schnell`.
- Salad service: `flux_schnell`.
- Queue: `ai-video-factory-flux-schnell-jobs`.
- GPU: RTX 4090 (24 GB).
- Autoscaler: min replicas 0, max replicas 1.
- Generation profile: `flux1-schnell-bnb4-v1`.
- Transformer and T5 encoder are loaded with bitsandbytes NF4 4-bit quantization.
- Generation is deterministic for the application contract: job ID, seed, prompt rendering, dimensions and four inference steps are fixed.

The Hugging Face repository is gated. The account behind `HF_TOKEN` must have access before the first worker bootstrap.

## Phase 4 planning

`audit_phase4_reference_cache.py` performs a read-only R2 plan before GPU allocation:

- `hit`: an Ideogram or FLUX artifact is already cached; no queue work.
- `miss`: at least one executable Ideogram variant has not been safety-rejected; Ideogram remains primary.
- `safety_blocked`: all executable Ideogram variants are present in the negative safety cache and no FLUX artifact exists; skip Ideogram and use FLUX.
- `invalid`: cached metadata or content integrity is invalid; stop before GPU allocation.

`run_phase4_assets_controlled.ps1` consumes that plan. A safety-only batch does not prewarm Ideogram. It starts the FLUX container group in scale-to-zero mode, submits only the required fallback jobs, then stops the group and cleans its queue.

## Phase 6 behavior

Phase 6 prewarms Ideogram as before. The FLUX group is placed in running scale-to-zero state with zero idle replicas. If a keyframe hits a terminal Ideogram safety rejection, the fallback queue autoscaler allocates one FLUX worker. The controlled wrapper stops and cleans both services at the end.

## Cache and provenance

FLUX output uses its own deterministic application job IDs (`flux-reference-*` / `flux-keyframe-*`) and R2 objects. Generated metadata records:

- `provider=flux1_schnell`
- `model=black-forest-labs/FLUX.1-schnell`
- `fallback_from=ideogram4`
- `fallback_reason=safety_rejection`
- application job ID, request SHA-256 and replay state

This keeps fallback outputs auditable and prevents repeated paid inference after a successful generation.

## Deployment

Use the manifest-authoritative Prepare wrapper for the first deployment:

```powershell
.\scripts\prepare_salad_worker_manifest.ps1 `
  -Service flux_schnell `
  -PrepareTimeoutMinutes 120 `
  -NonInteractive
```

Do not use `-SkipBuild` for the first FLUX deployment. The wrapper restores local process environment values after Prepare, so a development `.env` may keep `SALAD_QUEUE_ENABLED=false` without overriding the Salad manifest.
