# Production runner

The supported production path is a single host-side command. It accepts a finished script, reuses
valid cached artifacts, starts Salad workers only when required and always performs final cleanup.

## Run

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\run_video_factory.ps1 `
    -Input .\data\input\script.txt `
    -NonInteractive
```

The runner performs:

```text
preflight
  -> resumable phases 2-8 DAG
  -> Phase 9 Remotion + FFmpeg composition
  -> FinalVideo validation
  -> stop all project GPU services
  -> verify replicas=0
  -> clean queues
```

Model inference stays on Salad. The Windows host only runs orchestration, OpenAI planning,
Remotion and FFmpeg.

## Active DAG

```text
phase2-narrative
  |-----------------------------|
  v                             v
phase3-continuity         phase5-narration [Breeze -> Fish]
  |------------|                 |
  v            v                 v
phase3-shots  phase4-prompts   phase5-alignment [Whisper]
  |            |                 |
  |            +-> phase4-assets [Qwen-Image-2.1]
  |                              |
  |                        phase5-beat-timing
  |                              |
  +---------------------- phase5-shot-timing
                                 |
                        phase6-storyboard
                         |             |
                         v             v
                phase8-video-prompts  phase6-keyframes [Qwen-Image-2.1]
                         |             |
                         +------|------+
                                v
                       phase8-videos [LTX-2.5]
                                |
                                v
                    phase8-upscale [Real-ESRGAN]
                                |
                                v
                  Phase 9 -> FinalVideo
```

The scheduler defaults to four concurrent stages and two concurrent GPU-backed stages. Shared
service resource keys prevent conflicting Qwen work from overlapping.

## Cache and resume

`data/output/production_manifest.json` stores deterministic stage fingerprints. Valid outputs are
skipped or adopted; changed inputs invalidate only the affected stage and downstream dependencies.

GPU-backed stages validate R2 metadata before prewarm. A valid cache hit therefore avoids GPU
allocation entirely. Phase 8 also keeps its transport manifest so interrupted LTX jobs can be
reconciled or resubmitted with the same deterministic application ID.

## Fail-fast preflight

`scripts/preflight_video_factory.py` runs before GPU allocation and validates the source file,
manifest, required local tools, Salad/R2/Postgres connectivity, OpenAI access, Hugging Face access
and queue safety. Production does not use `--skip-network`.

## Debugging

Inspect without mutation:

```bash
python scripts/run_production.py data/input/script.txt --plan
```

Run serially:

```bash
python scripts/run_production.py data/input/script.txt --serial
```

Stop after a stage:

```bash
python scripts/run_production.py data/input/script.txt --through phase6-keyframes
```

Force one stage from the normal wrapper:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\run_video_factory.ps1 `
    -Input .\data\input\script.txt `
    -ForceStage phase6-keyframes `
    -NonInteractive
```

Individual phase runners and controlled GPU wrappers remain available only for targeted debugging
and validation.

## Failure behavior

Configuration, malformed cache metadata and control-plane failures fail explicitly. Breeze may use
Fish Speech only for the classified fallback conditions implemented by the narration workflow.
Dependent DAG stages are not scheduled after an upstream failure. The outer wrapper always attempts
to stop project workers, verify zero replicas and clean queues.

## Outputs

The final video is written to `data/output/phase9/final_video.mp4`.

`production_metrics.json` records stage timings, outcomes, concurrency and critical-path metrics.
`video_factory_metrics.json` records total wall-clock time, Phase 9 time, final output and cleanup
state.
