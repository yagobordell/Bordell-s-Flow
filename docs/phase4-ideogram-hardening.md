# Phase 4 — Ideogram hardening

This note records the safeguards added after validating Phase 4 references through the Salad-hosted Ideogram 4 worker.

## Safety-neutral location prompts

`VisualReferenceBot` keeps two distinct representations of a continuity entity:

- `description`: rich visual identity used by the textual planning layer.
- `safe_generation_description`: concise physical description sent to the image generator.

For locations, the generation description must contain only stable visible geography, architecture, materials, layout and physical landmarks. Narrative state, history, danger, abandonment, damage, conflict, dramatization, concrete people, actions and events are excluded before GPU inference.

The Ideogram caption template also avoids duplicating the generated location description in the background field.

## Deterministic fallback and cache reuse

Phase 4 location references have at most two deterministic Ideogram request variants:

1. `canonical`
2. `safe_fallback`

The fallback is structural: it retains the concise physical location subject but replaces style/background with a fixed neutral reference profile. It does not maintain an open-ended word blacklist.

Before any Salad queue submission the provider checks R2 for both deterministic variants. A previously successful fallback is therefore replayed from object storage without paying for the known-blocked canonical request again.

`ReferenceAsset.metadata` records the selected `prompt_variant`, application `job_id`, `request_sha256` and whether the result was replayed.

`audit_phase4_reference_cache.py` follows the same variant ordering. It remains R2-only, reports the matched variant and accepts UTF-8 files with or without BOM.

## Ideogram model bootstrap watchdog

Ideogram model weights remain downloaded at replica startup and `.ready` is still written only after the required safetensor files exist.

The Hugging Face download now runs under a byte-progress watchdog:

- network download timeout: 120 s
- metadata/etag timeout: 30 s
- progress polling: 15 s
- no-byte-progress timeout: 600 s
- absolute download timeout: 1800 s

The watchdog measures bytes under the local model snapshot, not the `N/27` file counter. Large files may therefore continue downloading for as long as bytes keep increasing. A replica that makes no byte progress for ten minutes exits, allowing Salad to replace the unhealthy host instead of keeping an RTX 4090 allocated indefinitely.

## Cold-start pending budget

The Salad transport job remains `pending` while Salad finds capacity and while a fresh Ideogram replica downloads weights and prepares the runtime. These are separate phases, but the client observes one pending interval.

A validated cold start showed roughly 35 minutes waiting for Salad capacity followed by about 19 minutes of healthy byte-progress model download. A 60-minute client pending timeout therefore expired only a few minutes after the model bootstrap completed, before the runtime became ready.

Phase 4 references and Phase 6 keyframes now default to a 5400-second (90-minute) pending timeout. The dispatched/running timeout remains independent. This larger pending budget does not remove the worker safeguards: model download still has the 10-minute no-progress watchdog and the 30-minute absolute download limit.

## Deployment

The hardened worker image is versioned as:

```text
docker.io/yagobordell/ai-video-factory:ideogram4-nf4-quality48-v2
```

The Ideogram deployment remains scale-to-zero with `min_replicas=0`, `max_replicas=1` and high priority.

Because the worker/bootstrap code changed, this revision requires exactly one Ideogram `Prepare` after merging and pulling the change. `Prepare` must finish with the group stopped at zero replicas before any generation job is submitted.

The later 90-minute client pending-budget adjustment is orchestration-only and does not require another Ideogram image build or `Prepare`.

LTX is not affected by this change.
