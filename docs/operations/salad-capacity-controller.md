# Salad Capacity Controller

Production uses one long-lived Capacity Controller for the entire Salad project. Video runs publish
jobs to Postgres; they do not start, stop or resize shared container groups.

## Database prerequisites

Apply these migrations before starting the controller:

```text
infra/sql/003_gpu_job_runtime_autoscaling.sql
infra/sql/004_salad_capacity_controller_coordination.sql
```

Migration 004 creates `gpu.capacity_controller_state`. The controller also holds a session-scoped
PostgreSQL advisory lock. A second controller process exits instead of competing for replica control.
If the leader process or database session dies, PostgreSQL releases the advisory lock automatically.

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
failures eventually make health fail.

## Production ownership

The Capacity Controller is the only production component allowed to decide project replica counts.
It calculates demand from all active `gpu.jobs`, protects running instances with deletion cost, and
converges groups to zero only after aggregate demand disappears.

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
