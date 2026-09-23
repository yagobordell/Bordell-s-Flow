# Architecture

Bordell's Flow is a modular video-production pipeline. The production entry point is a finished
script; experimental topic-to-script generation and historical benchmark infrastructure are not part
of the active architecture.

## Production flow

```text
SourceScript
  -> narrative planning
  -> continuity + shots
  -> narration + timing
  -> visual references
  -> storyboard + keyframes
  -> LTX-2.5 video generation
  -> Real-ESRGAN x2 upscale
  -> Remotion + FFmpeg composition
  -> FinalVideo
```

The default media providers are:

- OpenAI structured reasoning for planning tasks.
- Breeze TTS 2 on Salad for narration.
- Fish Speech S2 Pro on Salad as the classified narration fallback.
- Whisper Large V3 Turbo on Salad for transcription/alignment.
- Qwen-Image-2.1 on Salad for production image generation.
- LTX-2.5 on Salad for video generation, including the audio-to-video avatar path.
- Real-ESRGAN on Salad for 2x video upscale.
- Ideogram 4 remains implemented for explicit future use but is outside the normal production path.

## Repository boundaries

```text
src/ai_video_factory/
  bots/         structured planning logic
  compositor/   final timeline, Remotion and mux integration
  domain/       canonical audiovisual contracts
  inference/    provider-neutral remote job/storage/lease core
  providers/    external provider adapters
  workers/      model-specific Salad runtimes
  workflows/    application workflows and resumable orchestration

scripts/        executable orchestration and operational tooling
deploy/salad/   canonical Salad service definitions
docker/workers/ model-specific worker images
remotion/       isolated TypeScript renderer
infra/sql/      persistent inference job schema
tests/          regression and contract coverage
```

## Canonical contracts

The production domain intentionally stays small. Operational transport metadata does not leak into
the audiovisual contracts.

```text
SourceScript       = { text }
NarrativeBlock     = { id, text }
Beat               = { id, block_id, action }
Scene              = { id, beat_ids }
ContinuityEntity   = { id, kind, name, description }
BlockContinuity    = { block_id, entity_ids }
Shot               = { id, scene_id, beat_ids, entity_ids, action }
VisualReference    = { entity_id, prompt }
ReferenceAsset     = { entity_id, uri, metadata }
NarrationAudio     = { uri, duration_seconds }
NarrationWord      = { id, text, start_seconds, end_seconds }
BeatTiming         = { beat_id, start_word_id, end_word_id, start_seconds, end_seconds }
ShotTiming         = { shot_id, start_seconds, end_seconds }
StoryboardFrame    = { shot_id, prompt }
StoryboardKeyframe = { shot_id, uri }
StoryboardGrid     = { scene_id, uri }
VideoPrompt        = { shot_id, prompt }
VideoClip          = { shot_id, uri }
FinalVideo         = { uri, duration_seconds }
```

## Inference boundary

All self-hosted model workers use the shared inference core:

```text
orchestrator
   |
   v
Salad Job Queue
   |
   v
model-specific worker
   |             \
   v              v
R2 artifacts   Postgres job state / leases
```

`ai_video_factory.inference` owns the provider-neutral job envelope, object storage interfaces,
idempotency, leases, replay and task dispatch. Model-specific code lives under
`ai_video_factory.workers` and registers only the tasks it supports.

The canonical deployment inventory is `deploy/salad/services.json`. Production workers scale to zero
when idle and are started only by controlled orchestration when cache checks show work is required.

## Image generation

Qwen-Image-2.1 is the active production image generator for Phase 4 references and Phase 6
keyframes. Both paths use deterministic application job identities and persist results in R2 before
downstream use.

Ideogram remains a separate explicit implementation for future or intentionally selected workflows.
It is not a silent production fallback.

## Narration and alignment

Breeze TTS 2 is the primary narration worker. Fish Speech is started only for classified Breeze
failures; it is not prewarmed speculatively.

Whisper performs word-level alignment from the canonical narration asset. Timing workflows derive
`BeatTiming` and `ShotTiming` deterministically from those words.

## Video generation and upscale

LTX-2.5 consumes storyboard keyframes and motion prompts for normal image-to-video generation.
The same dedicated worker also exposes the audio-to-video task used by avatar segments. LTX output
uses the production 1280x720 / 24 fps contract.

Real-ESRGAN then upscales accepted clips 2x to 2560x1440 while preserving timeline semantics.

## Composition

Phase 9 treats `ShotTiming` as the source of truth. Remotion renders deterministic visual motion and
FFmpeg performs the final audio mux. The final mux preserves the accepted video stream instead of
regenerating model output.

## Orchestration

`scripts/run_video_factory.ps1` is the normal end-to-end entry point. It performs:

1. configuration and queue preflight before GPU allocation;
2. cache/resume inspection;
3. bounded DAG execution for production stages;
4. controlled Salad worker lifecycle;
5. Phase 9 composition;
6. final stop, zero-replica verification and queue cleanup.

`scripts/run_production.py` owns stage fingerprints and resume semantics. Persisted artifacts remain
the source of production content; manifests contain only operational state.

## Design rules

- Keep model-specific code out of the provider-neutral inference core.
- Keep production contracts independent of Salad/R2/Postgres transport details.
- Prefer deterministic job identities and cache-before-GPU checks.
- Do not keep historical compatibility layers when the active path has a dedicated replacement.
- Historical benchmarks, migrations and validation evidence belong in Git history rather than in the
  active source tree unless they are still required to operate or reproduce the current system.
