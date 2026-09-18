# Phase 4 — Formal production closure

Status: **CLOSED**

Phase 4 converts canonical continuity entities into reusable visual references and persisted PNG assets.
The phase is formally closed after production validation of the Ideogram 4 primary path, the
FLUX.2 Klein 4B safety-only fallback, deterministic R2 replay, zero-GPU cache execution and safe
Salad scale-to-zero cleanup.

Validation date: **2026-09-17**.

## Scope completed

```text
4.1  VisualReference planning                         CLOSED
4.2  Ideogram 4 production generation               CLOSED
4.3  deterministic negative safety cache            CLOSED
4.4  FLUX.2 Klein 4B safety-only fallback             CLOSED
4.5  R2 content-integrity audit                      CLOSED
4.6  ready-before-queue GPU lifecycle                CLOSED
4.7  scale-to-zero restore + stale queue handling    CLOSED
4.8  zero-GPU deterministic replay                   CLOSED
```

## Canonical domain boundary

The audiovisual contract remains intentionally small:

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

Provider, model, prompt variant, hashes, queue IDs and fallback provenance live in operational
metadata and do not expand the canonical domain model.

## Production provider policy

Ideogram 4 is the primary reference-image provider.

FLUX.2 Klein 4B is a fallback **only** when all executable Ideogram variants for one entity have
already reached confirmed terminal safety rejection. It is not used for network errors, timeouts,
OOMs, bootstrap failures, queue failures or capacity problems.

The validated Phase 4 Ideogram safety sequence is:

```text
canonical
safe_simplified
safe_minimal_art
```

A terminal safety result is persisted in R2 as negative cache evidence. A later execution replays
that evidence and skips the corresponding queue submission.

## Validated production run

The closure run used the canonical one-minute E2E references under:

```text
data/output/e2e-one-minute-20260915/phase4/
```

The final reference set contains nine PNGs:

```text
8 references -> Ideogram 4
1 reference  -> FLUX.2 Klein 4B safety fallback
9/9          -> persisted and SHA-256 verified in R2
```

The only fallback entity was:

```text
entity:       location_004
provider:     flux2_klein
model:        black-forest-labs/FLUX.2 Klein 4B
fallback_from: ideogram4
fallback_reason: safety_rejection
job_id:       flux-reference-e7b14bc64f8943ee07421a17ac155257
```

Its three Ideogram safety rejections were replayed from cache:

```text
canonical        -> ideogram-reference-87d91887e60f8921dda523199fcf30fa
safe_simplified  -> ideogram-reference-8bca6e070e7697432769fa1a61f08232
safe_minimal_art -> ideogram-reference-a162b385342b2b48b0e73c91f7b5c816
```

The final local artifact set was:

```text
reference_assets/
├── location_001.png
├── location_002.png
├── location_003.png
├── location_004.png
├── object_001.png
├── object_002.png
├── object_003.png
├── object_004.png
└── object_005.png
```

## Content-integrity audit

The closure audit command was:

```powershell
python scripts/audit_phase4_reference_cache.py `
  "data/output/e2e-one-minute-20260915/phase4/visual_references.json" `
  --verify-content-sha256 `
  --json-output "data/output/e2e-one-minute-20260915/phase4/cache-audit-final.json"
```

Validated result:

```text
hits=9
misses=0
safety_blocked=0
invalid=0
total=9
mode=R2 HEAD + content SHA-256 verification
Salad queue submissions=0
```

This proves that all nine final artifacts exist with matching stored content identity.

## Zero-GPU replay validation

A second complete controlled invocation was executed after all nine outputs were cached.

The planning stage reported:

```text
hits=9 misses=0 safety_blocked=0 invalid=0 total=9
Phase 4 GPU plan: ideogram=False flux2_klein=False cached=9
All Phase 4 references are cached; no GPU allocation required.
```

The generation stage then rebuilt the local `ReferenceAsset[]` output entirely from persisted cache
state. The three known Ideogram rejections for `location_004` were replayed with `queue submission
skipped` and no Salad transport was created.

This is the required idempotence property for an expensive production stage: unchanged canonical
inputs produce the same artifacts without allocating a GPU or resubmitting completed work.

## FLUX worker validated configuration

The production fallback worker is:

```text
service:        flux2_klein
queue:          ai-video-factory-flux2-klein-jobs
model:          black-forest-labs/FLUX.2 Klein 4B
GPU:            RTX 4090 (24 GB)
image tag:      flux2-klein-4b-bf16-v1
image digest:   sha256:de8caee7704e92c4f519b66cb8ed32c11069488ae30557d78b4fe3cfe1d1c2e5
min replicas:   0
max replicas:   1
```

The worker uses bitsandbytes NF4 quantization for the large FLUX/T5 components.

Cold bootstrap downloads only the Diffusers runtime layout required by the production loader:

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

Unused top-level monolithic checkpoints are excluded. The validated selective snapshot was about
33.7 GB. The image explicitly includes `sentencepiece` and `protobuf`, both required by the FLUX/T5
tokenizer path, and the Docker build smoke-check imports them before publication.

## Ready-before-queue lifecycle

The controlled Phase 4 runner performs a read-only R2 plan before GPU allocation.

If FLUX is required, the runner:

1. verifies that no enumerable pending/running FLUX queue work exists;
2. requests exactly one replica;
3. starts the stopped container group;
4. waits for one instance with `started=True` and `ready=True`;
5. holds one ready replica while fallback queue work is submitted;
6. only then enters Phase 4 generation.

The validated transition reached:

```text
started=True ready=True
FLUX prewarm complete: one ready replica held for fallback queue work.
```

Therefore cold model bootstrap no longer consumes the inference transport job's pending timeout.

## Scale-to-zero and queue cleanup

The controlled runner always restores FLUX in `finally`.

The final required invariant is:

```text
Status:         stopped
replicas:       0
pending_change: False
```

Salad may preserve or temporarily raise the desired `replicas` field while a group is stopped.
The restore path therefore applies the manifest queue autoscaler and, when necessary, normalizes
`replicas=0` with a separate merge patch before final stop verification.

Salad queue summary metadata was also observed to remain stale after the only transport job had
already reached a terminal state. Cleanup therefore treats exhaustive enumerable job state as the
source of truth for active work:

- `pending` and `running` are active;
- terminal jobs are not active;
- a nonzero summary is accepted as stale only after exhaustive pagination completes and finds no
  pending/running jobs;
- incomplete pagination remains a hard failure.

No `Repair` action is part of the normal Phase 4 lifecycle.

## Operational commands

Canonical controlled generation:

```powershell
.\scripts\run_phase4_assets_controlled.ps1 `
  -ReferencesFile "data/output/e2e-one-minute-20260915/phase4/visual_references.json" `
  -OutputDir "data/output/e2e-one-minute-20260915/phase4/reference_assets" `
  -Metadata "data/output/e2e-one-minute-20260915/phase4/reference_assets.json" `
  -PrewarmTimeoutMinutes 60 `
  -PendingTimeoutSeconds 300 `
  -RunningTimeoutSeconds 1200 `
  -FluxPendingTimeoutSeconds 1800 `
  -PollSeconds 5 `
  -NonInteractive
```

FLUX image/config preparation is an upgrade operation, not a per-run step:

```powershell
.\scripts\prepare_salad_worker_manifest.ps1 `
  -Service flux2_klein `
  -PrepareTimeoutMinutes 120 `
  -NonInteractive
```

## Closure decision

All Phase 4 production requirements are implemented and validated:

- primary Ideogram generation;
- bounded deterministic safety variants;
- negative safety cache;
- safety-only FLUX fallback;
- deterministic application identity and provenance;
- R2 SHA-256 verification;
- ready-before-queue lifecycle;
- scale-to-zero restoration;
- conservative stale-queue handling;
- complete zero-GPU replay.

**Phase 4 is formally closed.**

The next unfinished roadmap stage for the repository as a whole is **Phase 10 — Verification agents**.
Phase 10 consumes the `FinalVideo` produced by the already-closed Phase 9 and should request selective
regeneration through earlier canonical stages only when a verification decision requires it.
