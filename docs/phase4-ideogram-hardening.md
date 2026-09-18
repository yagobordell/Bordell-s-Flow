# Phase 4 — Ideogram hardening

This note records the safeguards added after validating Phase 4 references through the Salad-hosted
Ideogram 4 worker and the final production behavior validated on 2026-09-17.

## Safety-neutral location prompts

`VisualReferenceBot` keeps two distinct representations of a continuity entity:

- `description`: rich visual identity used by the textual planning layer.
- `safe_generation_description`: concise physical description sent to the image generator.

For locations, the generation description must contain only stable visible geography, architecture,
materials, layout and physical landmarks. Narrative state, history, danger, abandonment, damage,
conflict, dramatization, concrete people, actions and events are excluded before GPU inference.

The Ideogram caption template also avoids duplicating the generated location description in the
background field.

## Deterministic safety variants and negative cache

Phase 4 location references use a bounded deterministic Ideogram sequence:

1. `canonical`
2. `safe_simplified`
3. `safe_minimal_art`

The variants progressively reduce descriptive/style complexity while preserving the physical identity
needed downstream. The sequence is finite; it is not an open-ended word blacklist or arbitrary retry
loop.

A terminal Ideogram safety rejection is persisted as negative cache evidence. Before any Salad queue
submission, the workflow checks R2 for both positive artifacts and known terminal safety results.
Therefore a later run does not pay again for already-known blocked requests.

Once all three executable Ideogram variants are terminal safety rejections, the entity becomes
`safety_blocked` for Ideogram and is eligible for the dedicated FLUX.2 Klein 4B safety fallback.
Infrastructure errors do not enter this state.

`ReferenceAsset.metadata` records the selected provider/variant, application `job_id`,
`request_sha256`, prompt identity and replay state. FLUX fallback metadata also records
`fallback_from=ideogram4` and `fallback_reason=safety_rejection`.

`audit_phase4_reference_cache.py` follows the same deterministic ordering. It can perform a metadata
HEAD-only planning pass before GPU allocation and a full content SHA-256 verification pass for final
closure.

## Ideogram model bootstrap watchdog

Ideogram model weights remain downloaded at replica startup and `.ready` is written only after the
required model files exist.

The Hugging Face download runs under a byte-progress watchdog:

- network download timeout: 120 s
- metadata/etag timeout: 30 s
- progress polling: 15 s
- no-byte-progress timeout: 600 s
- absolute download timeout: 1800 s

The watchdog measures bytes under the local model snapshot rather than trusting a file counter. Large
files may therefore continue downloading while bytes keep increasing. A replica that makes no byte
progress for ten minutes exits so Salad can replace an unhealthy host instead of keeping an RTX 4090
allocated indefinitely.

## Ready-before-queue execution

Cold start and queue wait are separate lifecycle phases.

Production Phase 4 and Phase 6 execution must use the controlled PowerShell runners. Before a provider
needs queue work, the control plane obtains a real ready replica and waits until Salad reports
`started=True` and `ready=True`.

Only after the worker is ready does the controlled runner submit inference. Capacity wait, model
download and runtime preparation therefore do not consume the inference transport job's pending
deadline.

The raw Python clients keep bounded pending/running deadlines and fail closed instead of leaving
unknown queued work alive indefinitely.

Canonical commands:

```powershell
.\scripts\run_phase4_assets_controlled.ps1 `
    -ReferencesFile <visual_references.json> `
    -OutputDir <reference-assets-dir> `
    -Metadata <reference-assets.json> `
    -NonInteractive
```

```powershell
.\scripts\run_phase6_keyframes_controlled.ps1 `
    -Frames <storyboard_frames.json> `
    -Shots <shots.json> `
    -OutputDir <keyframes-dir> `
    -Output <storyboard_keyframes.json> `
    -NonInteractive
```

Do not manually allocate providers before these runners. The controlled workflow owns provider
planning, prewarm, queue submission, cleanup and scale-to-zero restoration.

## Phase 4 provider planning

The Phase 4 wrapper first executes a read-only R2 audit and derives a provider plan:

```text
hit             -> replay cached positive artifact
miss            -> Ideogram remains primary
safety_blocked  -> FLUX fallback required
invalid         -> fail before GPU allocation
```

This allows a fully cached run to complete with:

```text
ideogram=False
flux2_klein=False
cached=<all references>
```

No GPU is allocated in that case.

## Controlled cleanup

The controlled runners execute provider restore/cleanup in `finally` and preserve the original
inference failure if cleanup itself encounters a secondary error.

Normal FLUX cleanup requires:

```text
status=stopped
replicas=0
pending_change=False
```

A stale nonzero Salad queue summary is not enough to prove active work. When necessary, cleanup
exhaustively enumerates queue jobs and considers only `pending`/`running` jobs active. Incomplete
enumeration remains a hard failure.

`Repair` is not part of the normal Phase 4 or Phase 6 lifecycle.

## Deployment

The current Ideogram service remains manifest-authoritative, scale-to-zero and digest-pinned. The
service manifest uses the production Ideogram image family `ideogram4-nf4-quality48-v4`, high
priority, `min_replicas=0` and `max_replicas=1`.

A worker image/configuration change requires one intentional `Prepare`; normal generation runs should
reuse the already prepared group.

FLUX fallback deployment and lifecycle are documented separately in
[`flux2-klein-safety-fallback.md`](flux2-klein-safety-fallback.md).

The complete real Phase 4 closure evidence is recorded in [`phase4-closure.md`](phase4-closure.md).
