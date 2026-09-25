# Salad Capacity Controller

Production uses one long-lived Capacity Controller for the entire Salad project. Video runs publish
jobs to Postgres; they do not start, stop or resize shared container groups. The manual Salad manager
also refuses `Prepare`, `Start` and `Stop` while `SALAD_AUTOSCALER_ENABLED=true` unless an
operator explicitly supplies `-AllowControllerOverride`.

## Database prerequisites

Apply these migrations before starting the controller:

```text
infra/sql/003_gpu_job_runtime_autoscaling.sql
infra/sql/004_salad_capacity_controller_coordination.sql
infra/sql/005_capacity_drain_provider_hold.sql
```

Migration 004 creates `gpu.capacity_controller_state`. The controller also holds a session-scoped
PostgreSQL advisory lock. A second controller process exits instead of competing for replica control.
If the leader process or database session dies, PostgreSQL releases the advisory lock automatically.

Set `SALAD_CAPACITY_CONTROLLER_POSTGRES_DSN` to a Direct or Session-mode Postgres URI when worker
traffic uses a transaction pooler. If it is omitted, the controller falls back to `POSTGRES_DSN`.
Known Supabase transaction-pooler URIs on port 6543 are rejected at startup because they cannot
preserve the session advisory lock.

## Run the leader

Load the production environment, set `SALAD_AUTOSCALER_ENABLED=true`, and run:

```bash
python scripts/salad/run_salad_capacity_controller.py --poll-seconds 15
```

Run this as a durable service/process independent of any individual video pipeline. Do not launch one
controller per production run.

Check health without touching Salad:

```bash
python scripts/salad/run_salad_capacity_controller.py --check-health
```

Health requires a fresh heartbeat and a recent successful reconciliation. A transient Salad API
failure may report `degraded` while remaining within the configured reconciliation grace; repeated
failures eventually make health fail. Failures while applying drain protection are reconciliation
failures rather than successful no-op passes. Safe downscale deferrals caused by incomplete
worker-to-instance mapping or insufficient observed idle instances are allowed to be transient, but
after `SALAD_AUTOSCALER_NONCONVERGENCE_FAILURE_POLLS` consecutive blocked reconciliations for the
same target they also fail reconciliation and allow health monitoring to degrade. Salad provider
transitions are also bounded: `pending_change=true` and the provider statuses `pending` or
`deploying` are treated as in-flight states, using the group's provider `update_time` as a
restart-safe age signal. If that transition exceeds
`SALAD_AUTOSCALER_PROVIDER_PENDING_MAX_SECONDS` (default two hours), reconciliation fails so
controller health degrades instead of remaining green indefinitely. If `update_time` is unavailable,
the leader falls back to the first transition observation for that process. Terminal provider states
`failed` and `succeeded` are never counted as healthy worker capacity; they fail reconciliation
immediately, and unknown provider states fail closed.

## Production ownership

The Capacity Controller is the only production component allowed to decide project capacity.
At the start of every reconciliation it lists the project's Container Groups and requires every
remote group to be declared in `deploy/salad/services.json`. Unknown groups are treated as control-
plane drift and fail reconciliation, preventing unaccounted GPU usage from bypassing the project
budget. It calculates demand from all active `gpu.jobs`, protects running instances with deletion
cost, and
stops a group after aggregate demand reaches zero. A stopped group is treated as zero effective
capacity even if Salad preserves a non-zero configured `replicas` value for the next start. Capacity
reductions are never credited to the project budget merely because a PATCH or stop request was
accepted: the controller keeps the previously observed capacity reserved until a later reconciliation
confirms the change. While Salad reports `pending_change=true` or the group is `pending` /
`deploying`, effective capacity is conservatively computed as the maximum of the configured replica
count and the number of still-listed instances. The controller is read-only for that group during
the provider transition: it sends no additional resize, start or stop request until Salad settles.
This also survives a controller restart during a partial downscale. If the live instance list cannot
be read while a provider transition is active, reconciliation fails closed instead of releasing quota.
Drain publication and worker claims share a transaction-scoped advisory lock per Salad instance,
closing the race where a worker could claim new work after that instance had been selected for
removal. Before the controller submits a resize or stop, those selected drains are promoted to a
persistent provider hold. While Salad reports `pending_change=true`, the hold remains claim-blocking
even after the ordinary drain TTL expires and even if demand rebounds. The hold is cleared only
after a later reconciliation observes that the provider change is no longer pending and the current
capacity can be treated as authoritative.

`run_video_factory.ps1` requires a healthy controller before the DAG starts. The Python production
runner checks controller health every 15 seconds while stages are active and cancels running stage
process trees if control-plane health becomes stale.

## Capacity tuning

Set an explicit `SALAD_AUTOSCALER_PROJECT_MAX_REPLICAS` for the production cost/SLA budget. The
controller logs a warning when the project cap is at least the sum of all per-service maxima and
therefore provides no additional project-wide constraint.

The global cold-start default is `SALAD_AUTOSCALER_COLD_START_SECONDS`. Large services can override
it independently, for example:

```text
SALAD_AUTOSCALER_QWEN_IMAGE_21_COLD_START_SECONDS=...
SALAD_AUTOSCALER_LTX25_COLD_START_SECONDS=...
```

Calibrate these values from measured Salad allocation, download, model-load and readiness times
rather than guessing them.
