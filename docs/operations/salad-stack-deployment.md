# Salad stack deployment

## Source of truth

`deploy/salad/services.json` is the canonical deployment manifest.

Schema version 3 uses:

```json
{
  "stack": {
    "job_transport": "postgres"
  }
}
```

Each model has its own Salad container group. Postgres is the canonical job/control plane and R2 is
shared object storage. Salad provides compute only.

Do not duplicate mutable image tags, GPU classes, group names or capacity ceilings in scripts or
documentation.

## Validate

Run the stack manager in validation mode before touching remote resources:

```powershell
./scripts/salad/manage_salad_stack.ps1 -Action Validate
```

Validation checks manifest schema, service names, capacity bounds, GPU-class declarations and local
Dockerfile paths. It does not allocate a GPU.

## Prepare

`Prepare` builds/pushes an image unless `-SkipBuild` is used, resolves the immutable digest, resolves
GPU class names through Salad's organization API, and creates or updates the container group while
leaving it stopped at `replicas=0`.

A group created by the old architecture may still expose `queue_connection` or
`queue_autoscaler`. The manager never tries to partially patch those fields away. Migration is
allowed only when the group is already stopped at zero replicas; the manager then recreates the group
without queue state.

```powershell
./scripts/salad/manage_salad_worker.ps1 -Action Prepare -Service ltx25 -NonInteractive
```

Preparation verifies the activated immutable image, container priority, absence of legacy queue
attachments and zero replicas.

## Start

`Start` sets an explicit replica count and starts the group. The requested count must not exceed
`capacity.max_replicas`.

```powershell
./scripts/salad/manage_salad_worker.ps1 -Action Start -Service ltx25 -Replicas 4 -NonInteractive
```

The command waits until the group is running, the change has settled and the observed replica count
matches the request.

There is no queue-triggered autoscaling. The caller owns the capacity decision.

## Stop

`Stop` stops the group, patches replicas to zero when necessary, and requires repeated stable
`stopped / replicas=0 / pending_change=false` observations before returning success.

```powershell
./scripts/salad/manage_salad_worker.ps1 -Action Stop -Service ltx25 -NonInteractive
```

The full pipeline runs a final stack stop even after failures.

## Status

`Status` reports the group state, explicit capacity bounds, image and whether a legacy queue
attachment is still visible.

Any `legacy_queue=True` result is a migration issue: stop the service at zero replicas and run
`Prepare` before production use.

## Secrets and runtime configuration

The manager loads `.env` by default. Required secrets such as `POSTGRES_DSN`, R2 credentials,
`HF_TOKEN` and `SALAD_API_KEY` are injected into the worker environment according to the manifest.

Production workers poll Postgres directly. Do not add `SALAD_QUEUE_ENABLED`, per-model queue names
or queue-autoscaler configuration back into deployment state.
