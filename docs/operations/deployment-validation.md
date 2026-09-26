# Salad deployment validation

Use this runbook after changing a worker image, model bootstrap or Salad configuration.
The former end-to-end video runner was retired with the old planning bots;
these checks validate the independent Salad services and their Postgres/R2 jobs.

## Prerequisites

Required credentials come from `.env` and the selected service definition in
`deploy/salad/services.json`. Common requirements are Salad API access, Postgres, R2 and Hugging Face
credentials where the model requires them. The Capacity Controller must use a Direct or Session-mode
Postgres connection; configure `SALAD_CAPACITY_CONTROLLER_POSTGRES_DSN` when ordinary worker traffic
uses transaction pooling. Docker is needed only when building or publishing images.

## Commands

Validate, prepare and inspect a single service with the compute manager:

```powershell
.\scripts\salad\manage_salad_worker.ps1 -Service <service> -Action Validate
.\scripts\salad\manage_salad_worker.ps1 -Service <service> -Action Prepare
.\scripts\salad\manage_salad_worker.ps1 -Service <service> -Action Status
```

For production-path validation, keep the global Capacity Controller running and submit the smoke
job through Postgres. The controller must own the resulting `0 -> N -> 0` replica lifecycle.

Explicit paid capacity is reserved for an isolated operator smoke when the global controller is
intentionally not managing that service:

```powershell
.\scripts\salad\manage_salad_worker.ps1 -Service <service> -Action Start -Replicas 1
```

Always stop isolated manual capacity afterwards:

```powershell
.\scripts\salad\manage_salad_worker.ps1 -Service <service> -Action Stop
```

Some model-specific controlled smoke scripts under `scripts/smoke/` still own this isolated
lifecycle. Do not run them against a service that the global Capacity Controller is actively
managing; use the controller-backed path for production certification.

## Acceptance

A worker change is accepted when:

- the intended immutable image/configuration is deployed;
- a real Postgres-backed inference succeeds;
- persisted R2 artifacts validate;
- the expected runtime operates without OOM/bootstrap failure;
- repository CI is green;
- the singleton controller remains healthy throughout the run;
- no second controller can acquire leadership;
- after aggregate Postgres demand disappears, the service converges to stable `stopped` with no pending change.

Cloud logs and generated smoke artifacts are evidence for that run. Git history is the historical
record; active docs should describe only the current architecture.

## Failure handling

Keep unrelated workers stopped. Inspect the application job in Postgres, the worker logs and the
container-group state. Do not clean or repair a provider queue: the production architecture has no
Salad Job Queue.

Do not extend timeouts to hide a stalled model download. Use worker watchdog/progress signals to
distinguish healthy transfer from a real stall.
