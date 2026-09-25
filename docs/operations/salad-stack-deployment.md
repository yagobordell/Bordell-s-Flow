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

Each model has its own unique Salad container group. The runtime rejects duplicate service-order
entries or duplicate `group_name` assignments before opening the Postgres control plane. Postgres is
the canonical job/control plane and R2 is shared object storage. Salad provides compute only.

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
leaving it stably stopped. A stopped group may still report a configured replica count; that value is
configuration for a future start, not effective running capacity.

A group created by the old architecture may still expose `queue_connection` or
`queue_autoscaler`. The manager never tries to partially patch those fields away. Migration is
allowed only when the group is stably stopped with no pending change; the manager then recreates the
group without queue state.

```powershell
./scripts/salad/manage_salad_worker.ps1 -Action Prepare -Service ltx25 -NonInteractive
```

Preparation verifies the activated immutable image, container priority, absence of legacy queue
attachments and a stable `stopped / pending_change=false` state.

## Start

`Start` sets an explicit replica count and starts the group. The requested count must not exceed
`capacity.max_replicas`.

```powershell
./scripts/salad/manage_salad_worker.ps1 -Action Start -Service ltx25 -Replicas 4 -NonInteractive
```

The command waits until the group is running, the change has settled and the observed replica count
matches the request.

There is no Salad Job Queue autoscaling. In production, the singleton Postgres-elected Capacity
Controller owns replica decisions. Explicit `Start`, `Stop` and `Prepare` acquire the same
session-scoped PostgreSQL advisory lock used by controller leadership and hold it for the full
mutation. A live controller therefore blocks manual capacity changes across hosts, and a manual
mutation blocks controller startup until it completes. `-AllowControllerOverride` intentionally
bypasses that coordination and is reserved for deliberate operator intervention.

## Stop

`Stop` uses Salad's explicit stop operation and requires repeated stable
`stopped / pending_change=false` observations before returning success. It does not rewrite the
configured replica count merely to prove that capacity is off.

```powershell
./scripts/salad/manage_salad_worker.ps1 -Action Stop -Service ltx25 -NonInteractive
```

Normal production pipelines never run a project-wide stop. The global Capacity Controller observes
aggregate Postgres demand and owns scale-to-zero. Manual `Stop` remains an operator/smoke command.

## Status

`Status` reports the group state, explicit capacity bounds, image and whether a legacy queue
attachment is still visible.

Any `legacy_queue=True` result is a migration issue: stop the service until it is stable and run
`Prepare` before production use.

## Secrets and runtime configuration

The manager loads `.env` by default. Required secrets such as `POSTGRES_DSN`, R2 credentials,
`HF_TOKEN` and `SALAD_API_KEY` are injected into the worker environment according to the manifest.

Production workers poll Postgres directly. Do not add `SALAD_QUEUE_ENABLED`, per-model queue names
or queue-autoscaler configuration back into deployment state.
