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

Account selection is **never** read from this manifest. Every operator must set
`SALAD_ORGANIZATION` and `SALAD_PROJECT` in their own `.env` (or process
environment), alongside `SALAD_API_KEY`. Non-secret application and Salad settings
live in `config/app.env` and `config/salad.env`. The PowerShell manager and Python
Capacity Controller load these files without overwriting existing process variables.
Account names must be URL-safe Salad
slugs. Missing or invalid account settings fail before any Salad API request.
`Validate` remains an offline schema check and does not require credentials.

`SALAD_QUEUE_NAME`, `SALAD_LOG_LEVEL` and `SALAD_QUEUE_ENABLED=false` are kept in
`config/salad.env` for compatibility. These settings
do not select the account or enable the removed Salad Job Queue; Postgres remains
the sole job transport. Do not turn `SALAD_QUEUE_ENABLED` on.

To run your own Salad organization without affecting production, use a separate
Postgres control plane and preferably a separate R2 bucket. Two independent Salad
accounts sharing the same `gpu.jobs` queue and Capacity Controller advisory lock
are **not** isolated merely by changing their API keys or account names.

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
mutation. The helper continuously verifies that its PostgreSQL session still owns that lock, while
the PowerShell manager performs a synchronous authority check before and after every Salad
`POST`, `PATCH` or `DELETE` and throughout long convergence waits. If the helper exits, the
database session is lost, or lock ownership disappears, the manual operation fails closed and emits
no further Salad mutations. A live controller therefore blocks manual capacity changes across
hosts, and a manual mutation blocks controller startup until it completes.
`-AllowControllerOverride` intentionally bypasses that coordination and is reserved for deliberate
operator intervention.

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

The manager loads `.env` and the files in `config/` by default. Set `SALAD_ORGANIZATION` and
`SALAD_PROJECT` for every deployment; the manifest has no account fallback.
Required secrets such as `POSTGRES_DSN`, R2 credentials,
`HF_TOKEN` and `SALAD_API_KEY` are injected into the worker environment according to the manifest.

Production workers poll Postgres directly. Do not add `SALAD_QUEUE_ENABLED`, per-model queue names
or queue-autoscaler configuration back into deployment state.


## Reviewed LTX A2V image drift during `run.ps1`

The pinned A2V image in `SALAD_LTX25_PAGEABLE_A2V_IMAGE` must match the
independent Salad `ltx25` worker exactly. A Git pull never redeploys a pinned
Docker image. A mismatch is not an inference retry and cannot be corrected by
simply starting the Container Group.

By default, `SALAD_LTX_AUTO_PREPARE_ON_MISMATCH=true`. When an A2V job is about
to be submitted, `run` checks the image. If it is already correct, the
existing controller owns all normal scaling. If the group reports a different
image, `run` calls the existing audited `manage_salad_worker.ps1 -Action Prepare`
**only if** the group is stably stopped with no pending Salad change, the
Capacity Controller is inactive, and Postgres has zero unfinished A2V jobs.
The manager rechecks stopped state and obtains the same session advisory lock
used by the controller; `-AllowControllerOverride` is never used. After
preparation, the runner verifies the pinned image and stopped state again.

If the controller is active, the worker is running/deploying, or Postgres has
unfinished A2V jobs, automatic replacement deliberately fails before paid
inference. Complete/drain existing jobs and schedule a maintenance interval:
stop the singleton Capacity Controller, then use the existing audited manager:

```powershell
.\\scripts\\salad\\manage_salad_worker.ps1 \`
  -Action Prepare -Service ltx25 \`
  -PinnedImage $env:SALAD_LTX25_PAGEABLE_A2V_IMAGE \`
  -EnvFile .env -NonInteractive
```

Restart the Capacity Controller and re-run `run.ps1 <script> <avatar>
--generate-video`. Validated audio and images are reused; no new paid LTX
work is submitted while the exact reviewed digest is missing. I2V deployment
remains independent and is not silently changed. Set
`SALAD_LTX_AUTO_PREPARE_ON_MISMATCH=false` to disable automatic preparation.
