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
Each wrapper invocation receives an isolated run ID and its own output/temp roots. Phase 9 remains
local composition; its shared Remotion staging directory is protected by a cross-process mutex.
The individual video run never stops project-wide GPU capacity.

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

`data/output/runs/<run-id>/production_manifest.json` stores deterministic stage fingerprints for
one wrapper invocation. Valid artifacts are reused within that run; changed inputs invalidate only
affected stages and their dependants. Direct `run_production.py` callers can choose their own
isolated root with `--output-dir`.

GPU stages inspect durable R2 state before starting Salad capacity. Inference jobs live in Postgres,
and deterministic application identity plus R2 metadata allow interrupted work to resume safely.

Workers commit each generated primary/sidecar set to a content-addressed internal R2 bundle before
publishing the public job keys. The ready manifest is durable recovery state: if publication stops
after one sidecar but before the primary, a retry completes the same committed bundle without
rerunning a non-deterministic model. Client replay of a Postgres `succeeded` row always revalidates
the complete R2 bundle; a missing artifact is recovered from that manifest when possible, otherwise
the client reports an incomplete bundle instead of returning success.

A committed bundle is also recovery authority after inference retries are exhausted. If Postgres is
`retryable_failed` or `failed` but the ready manifest validates, the client completes the final R2
publication and reconciles that same job to `succeeded` without incrementing `attempt_count` or
dispatching another model invocation. Active, pending and cancelled jobs are never rewritten by
this recovery-only path. A later cache hit retries the Postgres reconciliation if the R2 objects were
published successfully but the database update itself was temporarily unavailable.

### Internal bundle retention

The recovery objects live under `__ai_video_factory/bundles/`. The application intentionally does
not delete them: they remain the repair source for completed jobs whose public primary or sidecars
are later lost. Production must therefore define an R2 lifecycle policy for that prefix once the
required recovery window has been chosen from real incident/retry requirements and storage-volume
measurements. Apply retention to the whole bundle prefix consistently; expiring only manifests or
only staged objects destroys recovery guarantees. Until an explicit lifecycle policy is configured,
internal bundles are retained indefinitely.

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

Primary wrapper output:

```text
data/output/runs/<run-id>/phase9/final_video.mp4
```

Pass `-RunId <id>` to make the run identity stable for an intentional resume. When omitted, the
wrapper creates a unique ID so separate full-video executions cannot overwrite each other's local
manifests, metrics, phase artifacts or temporary files.

Replica cleanup belongs exclusively to the global Capacity Controller. When all Postgres demand is
gone, the controller converges the affected groups to zero. This prevents one video finishing from
stopping GPUs that are still serving another concurrent video.
