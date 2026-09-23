# Production runner

The supported end-to-end path accepts a finished script, reuses valid artifacts, starts GPU workers
only when needed and cleans them up on success or failure.

## Run

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\pipeline\run_video_factory.ps1 `
    -Input .\data\input\script.txt `
    -NonInteractive
```

The runner performs preflight, the resumable production DAG, Phase 9 composition, final validation,
worker shutdown, zero-replica verification and queue cleanup.

## Active DAG

```text
narrative
  |------------------------------|
  v                              v
continuity                 narration [Breeze -> Fish]
  |------------|                  |
  v            v                  v
shots      reference prompts   alignment [Whisper]
  |            |                  |
  |            v                  v
  |      references [Qwen]    shot timing
  |                               |
  +----------------------- storyboard
                          /          \
                         v            v
                 video prompts    keyframes [Qwen]
                          \          /
                           v        v
                          LTX videos
                              |
                              v
                       Real-ESRGAN x2
                              |
                              v
                    Remotion + FFmpeg
                              |
                              v
                         FinalVideo
```

## Cache and resume

`data/output/production_manifest.json` stores deterministic stage fingerprints. Valid artifacts are
reused; changed inputs invalidate only affected stages and their dependants.

GPU stages inspect durable R2 state before prewarming. Phase 8 also persists transport state so an
interrupted run can resume without changing logical application-job identity.

Inspect the plan without mutation:

```bash
python scripts/pipeline/run_production.py data/input/script.txt --plan
```

Useful debugging flags include `--serial` and `--through <stage>`. The PowerShell wrapper also
supports `-ForceStage` for targeted reruns.

## Performance measurement

Performance claims must use measured end-to-end wall time from the finished script to `FinalVideo`.
Do not report improvements by changing resolution, frame rate, model profile, validation, fallback
policy or output quality.

`production_metrics.json` records stage timing and critical-path data.
`video_factory_metrics.json` records total wall-clock time, Phase 9 time, final output and cleanup
state.

## Outputs

Primary output:

```text
data/output/phase9/final_video.mp4
```

The outer wrapper always attempts cleanup after a failure. A production run is not operationally
complete until project workers are stopped, replicas return to zero and queues are clean.
