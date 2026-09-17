# FLUX.1-schnell safety fallback

## Purpose

Ideogram 4 remains the primary open-weight image provider for Phase 4 references and Phase 6 storyboard keyframes. FLUX.1-schnell is a separate scale-to-zero fallback used only after Ideogram returns a terminal provider safety rejection for every executable deterministic caption variant.

The fallback exists for false-positive provider rejections such as the Phase 4 `location_004` desert mountain reference. It is not a general retry path for infrastructure failures.

## Provider policy

The image provider chain is:

1. read the deterministic Ideogram cache;
2. read the deterministic Ideogram safety-negative cache;
3. run Ideogram only when at least one executable variant is neither cached nor safety-blocked;
4. when Ideogram raises `Ideogram 4 safety filter blocked all provider caption variants`, read the deterministic FLUX cache;
5. submit one FLUX job only when its independent cache is missing.

`SafetyFallbackImageProvider` deliberately does **not** catch timeouts, queue failures, malformed requests, CUDA failures, bootstrap failures, or unrelated remote rejections. Those remain visible production errors.

Never delete a confirmed Ideogram safety-negative cache entry merely to retry the same fingerprint. A repeated request would pay for the same deterministic work and is expected to reproduce the same provider result.

## Model and license

The fallback is pinned to `black-forest-labs/FLUX.1-schnell`.

- Hugging Face model: `black-forest-labs/FLUX.1-schnell`
- license: Apache-2.0
- generation: guidance scale `0.0`, four inference steps, max sequence length `256`
- runtime compute: BF16
- large components: bitsandbytes 4-bit NF4 (`transformer` and `text_encoder_2`)
- target GPU: RTX 4090 24 GB

The Hugging Face repository requires accepting its access conditions before `HF_TOKEN` can download the files. The model card's out-of-scope use restrictions still apply to fallback generation; the fallback is not intended to bypass application or legal safety requirements.

## Deterministic cache identity

FLUX has its own application job namespace:

- Phase 4: `flux-reference-<sha256-prefix>`
- Phase 6: `flux-keyframe-<sha256-prefix>`

The job fingerprint includes the model, generation profile, rendered prompt, dimensions, and deterministic seed. Output remains under `jobs/<job_id>/image.png`, so it uses the existing R2 inference cache contract without colliding with Ideogram IDs.

Persisted image metadata records:

- `provider=flux_schnell`
- `model=black-forest-labs/FLUX.1-schnell`
- `fallback_from=ideogram4`
- `fallback_reason=safety_rejection`
- job ID, request SHA-256, and replay status

Phase 6 `StoryboardKeyframe` now preserves provider metadata just like Phase 4 `ReferenceAsset`.

## Salad deployment

Service: `flux_schnell`

- group: `ai-video-factory-flux-schnell-worker`
- queue: `ai-video-factory-flux-schnell-jobs`
- image: `docker.io/yagobordell/ai-video-factory:flux-schnell-bnb4-nf4-v1`
- GPU class: RTX 4090 (24 GB)
- autoscaler: `min_replicas=0`, `max_replicas=1`
- `SALAD_QUEUE_ENABLED=true` comes from `deploy/salad/services.json`

Prepare only this worker with the manifest-authoritative wrapper:

```powershell
.\scripts\prepare_salad_worker_manifest.ps1 `
  -Service flux_schnell `
  -PrepareTimeoutMinutes 120 `
  -NonInteractive
```

Do not use stack `Prepare` for this deployment path and do not prepare LTX, Breeze, Whisper, or Ideogram when adding the fallback.

After Prepare, the group must remain stopped at zero replicas. `HF_TOKEN` must be available to the Prepare operation and authorized for the FLUX.1-schnell repository.

## Cost-aware Phase 4 planning

`audit_phase4_reference_cache.py` now reports four effective states:

- `hit`: an Ideogram or FLUX output is already cached;
- `miss`: Ideogram still has an executable uncached candidate;
- `safety_blocked`: all executable Ideogram variants are negative-cached and FLUX is not cached;
- `invalid`: cached metadata/content does not satisfy the deterministic request contract.

`run_phase4_assets_controlled.ps1` runs the audit before allocating a GPU.

- If any record is a primary Ideogram `miss`, it performs the existing one-replica Ideogram prewarm and warm hold.
- If there are no executable Ideogram misses but there is a `safety_blocked` record, it skips Ideogram prewarm entirely. Submitting the FLUX fallback job lets the FLUX queue autoscaler cold-start its single replica.
- If all records are hits, no image GPU is prewarmed.

The FLUX pending timeout defaults to 1800 seconds so a scale-to-zero model can download/load before claiming the job. The normal Ideogram pending timeout remains 300 seconds because that path is explicitly prewarmed.

The `finally` path stops and cleans both relevant image services and prints their final status.

## Phase 6

Phase 6 uses the same provider chain and has a read-only `audit_phase6_keyframe_cache.py` planner. Known Ideogram safety blocks can therefore cold-start FLUX without first warming Ideogram. Provider provenance is persisted in `storyboard_keyframes.json`.

## Validation after first FLUX deployment

For the current Phase 4 dataset, the expected pre-generation plan is conceptually:

```text
8 Ideogram cache hits
1 Ideogram safety_blocked (location_004)
1 FLUX cache miss (location_004)
0 Salad submissions during audit
```

Run the controlled Phase 4 wrapper. It should skip Ideogram prewarm, submit exactly one FLUX fallback job, persist `location_004.png`, then return the FLUX group to zero replicas.

Finally run:

```powershell
python scripts/audit_phase4_reference_cache.py `
  "data/output/e2e-one-minute-20260915/phase4/visual_references.json" `
  --verify-content-sha256 `
  --json-output "data/output/e2e-one-minute-20260915/phase4/cache-audit-final.json"
```

The final target is:

```text
hits=9 misses=0 safety_blocked=0 invalid=0 total=9
Salad queue submissions=0
```

The `location_004` metadata should identify `provider=flux_schnell`, while the other eight assets continue to identify/replay their original Ideogram cache entries.
