# Salad deployment validation

Use this runbook after changing a worker image, model bootstrap or Salad configuration. Normal video
production should use `scripts/pipeline/run_video_factory.ps1`.

## Prerequisites

Required credentials come from `.env` and the selected service definition in
`deploy/salad/services.json`. Common requirements are Salad API access, Postgres, R2 and Hugging Face
credentials where the model requires them. Docker is needed only when building or publishing images.

## Commands

```powershell
.\scripts\salad\manage_salad_validation.ps1 -Service <service> -Action Validate
.\scripts\salad\manage_salad_validation.ps1 -Service <service> -Action Prepare
.\scripts\salad\manage_salad_validation.ps1 -Service <service> -Action Status
```

Use the dedicated command under `scripts/smoke/` for a real paid smoke instead of manually changing
replicas.

## Acceptance

A worker change is accepted when the intended immutable image/configuration is deployed, one real
queue-backed inference succeeds, persisted artifacts validate, the expected runtime operates without
OOM/bootstrap failure, repository CI is green and the service returns to zero replicas.

Cloud logs and generated smoke artifacts are evidence for that run. They should not be copied into
active documentation; Git history is the historical record.

## Failure handling

Keep unrelated workers stopped. Inspect the failing service and queue, preserve relevant logs, clean
stale transport jobs and fix the specific cause. Do not extend timeouts to hide a stalled model
download; use worker watchdog/progress signals to distinguish healthy transfer from a real stall.
