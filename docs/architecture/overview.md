# Architecture

Bordell's Flow turns a finished script into a reproducible 16:9 video.

## Production flow

```text
SourceScript
  -> narrative planning and shots
  -> narration + word alignment
  -> references + storyboard keyframes
  -> LTX 2.5 video (1280x720 @ 24 fps)
  -> Real-ESRGAN x2 (2560x1440 @ 24 fps)
  -> Remotion + FFmpeg
  -> FinalVideo
```

Production providers are OpenAI for structured planning, Breeze TTS 2 for narration, Fish Speech as
the classified narration fallback, Whisper Large V3 Turbo for alignment, Qwen Image 2.1 for images,
LTX 2.5 for video, Real-ESRGAN for upscale and Remotion/FFmpeg for composition.

Ideogram 4 remains implemented for explicit future use but is not selected by the normal production
route.

## Repository boundaries

```text
src/ai_video_factory/
  bots/         structured planning
  compositor/   final timeline and rendering contracts
  domain/       audiovisual contracts
  inference/    model-neutral remote job/storage/lease core
  providers/    provider adapters
  workers/      model-specific Salad runtimes
  workflows/    resumable application workflows

scripts/        pipeline, Salad, smoke and diagnostic commands
deploy/salad/   canonical Salad service manifest
docker/workers/ model worker images
remotion/       TypeScript renderer
infra/sql/      persistent inference-job schema
tests/          regression and contract coverage
```

## Core rules

- Domain contracts do not contain Salad, R2 or Postgres transport details.
- Model-specific imports stay outside `ai_video_factory.inference`.
- GPU jobs use deterministic application identities and cache/replay before new allocation.
- Qwen Image 2.1 is the active image generator; Ideogram is not a silent fallback.
- Breeze is primary narration; Fish Speech is used only for classified eligible failures.
- LTX output is silent 1280x720 video; Real-ESRGAN produces the 2560x1440 compositor input.
- Phase 9 uses `ShotTiming` as timeline truth and muxes narration without regenerating accepted video.
- Historical migrations and validation evidence belong in Git history, not active documentation.

## Orchestration

The supported end-to-end entry point is:

```text
scripts/pipeline/run_video_factory.ps1
```

It performs preflight, cache/resume inspection, dependency-aware execution, controlled Salad lifecycle,
composition and final cleanup. `scripts/pipeline/run_production.py` owns stage fingerprints and resume
semantics.

The canonical deployment inventory is `deploy/salad/services.json`.
