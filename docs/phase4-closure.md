# Phase 4 — Current production state

Status: **Qwen-only production path**

Phase 4 converts canonical continuity entities into reusable visual references and persisted PNG assets.
The active image-generation path is Qwen-Image-2.1 running on Salad with high priority on an RTX 5090
(32 GB). Ideogram remains implemented for future work, but it is not part of the active Phase 4 or
Phase 6 production route.

## Production contract

The canonical domain remains intentionally small:

```text
VisualReference = {
  entity_id,
  prompt
}

ReferenceAsset = {
  entity_id,
  uri
}
```

Provider-specific model IDs, request fingerprints, queue IDs and cache metadata remain operational
metadata and do not expand the canonical domain model.

## Qwen generation lifecycle

The controlled Phase 4 runner performs the following sequence:

1. validates the required local inputs;
2. verifies R2 connectivity before any GPU allocation;
3. audits deterministic Qwen cache keys in R2;
4. completes fully cached runs without allocating a GPU;
5. when work is missing, prewarms exactly one Qwen worker and holds it ready;
6. submits deterministic Qwen image jobs;
7. restores the service to scale-to-zero, cleans the queue and verifies final status.

The production image size is 1536x864 and generated artifacts are PNG.

## Cache and replay

`audit_phase4_reference_cache.py` builds exactly the same deterministic Qwen requests as the runtime
provider. A cache entry is accepted only when its stored application job ID, request fingerprint,
artifact digest and content type match the expected request.

A metadata-only audit can be run before GPU allocation. Content SHA-256 verification remains available
for deeper validation:

```powershell
python scripts/audit_phase4_reference_cache.py `
  "data/output/phase4/visual_references.json" `
  --verify-content-sha256 `
  --json-output "data/output/phase4/cache-audit.json"
```

## Controlled generation

Use the controlled runner for production generation:

```powershell
.\scripts\run_phase4_assets_controlled.ps1 `
  -ReferencesFile "data/output/phase4/visual_references.json" `
  -OutputDir "data/output/phase4/reference_assets" `
  -Metadata "data/output/phase4/reference_assets.json" `
  -NonInteractive
```

The corresponding Phase 6 keyframe runner follows the same Qwen cache/prewarm/cleanup policy.
