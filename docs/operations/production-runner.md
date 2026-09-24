# Production runner

The supported end-to-end path accepts a finished script, reuses valid artifacts and submits GPU
work to Postgres. A separate long-lived Salad Capacity Controller owns all shared replica changes.

## Run

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\scripts\pipeline\run_video_factory.ps1 `
    -Input .\data\input\script.txt `
    -NonInteractive
```

The runner performs preflight, verifies that the global Capacity Controller is healthy, runs the
resumable production DAG, and continuously re-checks controller health while stages are active.
Phase 9 remains local composition. The individual video run never stops project-wide GPU capacity.

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

GPU stages inspect durable R2 state before starting Salad capacity. Inference jobs live in Postgres,
and deterministic application identity plus R2 metadata allow interrupted work to resume safely.

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

Replica cleanup belongs exclusively to the global Capacity Controller. When all Postgres demand is
gone, the controller converges the affected groups to zero. This prevents one video finishing from
stopping GPUs that are still serving another concurrent video.
