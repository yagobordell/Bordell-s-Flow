# Salad deployment validation

This runbook is for validating a worker after its image, model snapshot or Salad configuration
changes. Normal video production should use `scripts/pipeline/run_video_factory.ps1` instead.

## Prerequisites

Provide the credentials required by `deploy/salad/services.json` in `.env`:

```text
SALAD_API_KEY
POSTGRES_DSN
R2_ENDPOINT_URL
R2_BUCKET
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
HF_TOKEN
```

Docker is required only when building/publishing worker images. It is not part of the runtime
control plane.

## Operator command

```powershell
.\scripts\manage_salad_validation.ps1 -Service <service> -Action <action>
```

Supported services and actions are defined by `deploy/salad/services.json` and the validation
manager. Use one service at a time when diagnosing a change so GPU cost and failure scope stay
bounded.

A typical validation cycle is:

```powershell
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Validate
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Prepare
.\scripts\manage_salad_validation.ps1 -Service ltx25 -Action Status
```

For a real smoke, use the current dedicated smoke command for that worker rather than manually
starting extra replicas. Always stop the service and verify zero replicas after paid validation.

## Acceptance

A worker change is accepted only when:

- the intended immutable image/configuration is deployed;
- one real queue-backed inference succeeds;
- R2 input/output metadata and SHA validation succeed;
- the expected GPU/runtime is used without OOM or bootstrap failure;
- the queue returns to an idle state;
- the container group returns to zero replicas;
- repository CI remains green.

Persisted cloud logs and generated validation artifacts are evidence for the run, but dated
validation reports do not belong in the active source tree; Git history is the historical record.

## Failure handling

Keep unrelated workers stopped, inspect the failing service status and queue, preserve relevant
container logs, clean stale transport jobs and fix one cause at a time. Do not increase timeouts to
hide a stalled model download; use the worker progress/watchdog metrics to distinguish healthy
transfer from a real stall.
