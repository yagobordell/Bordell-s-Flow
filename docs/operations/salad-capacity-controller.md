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
controller per production run. The normal reconciliation cadence remains the requested poll interval.
When a service reaches genuine zero demand and the controller has just published safe drain rows, the
next reconciliation is scheduled after the shorter drain grace (when that is below the normal poll
interval) so scale-to-zero does not pay an unnecessary full polling period.

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
same target **and reason** they also fail reconciliation and allow health monitoring to degrade.
The controller never stops a group while it cannot identify enough idle instances; once job demand
reaches zero and instances become observable, normal drain protection and stop reconciliation
resume. Salad provider
transitions are also bounded: `pending_change=true` and the provider statuses `pending` or
`deploying` are treated as in-flight states. Pending configuration changes retain the group's
provider `update_time` as their restart-safe age signal. A `deploying` group with
`pending_change=false` may retain an old group timestamp while Salad allocates a **new** worker:
only observed forward progress from its current-version instance (allocation, download progress,
creation or startup) refreshes the in-process watchdog. Merely polling an unchanged instance,
seeing a stale group timestamp, or listing zero instances does not count as progress. If the
provider transition has no progress for `SALAD_AUTOSCALER_PROVIDER_PENDING_MAX_SECONDS`
(default two hours), reconciliation fails so controller health degrades instead of remaining green
indefinitely. If `update_time` is unavailable, the leader falls back to the first transition
observation for that process. A controller restart can begin a new observed-bootstrap window for a
currently listed instance; this does **not** grant permission to resize a transitioning group.
Terminal provider states `failed` and `succeeded` are never counted as healthy worker capacity;
they fail reconciliation immediately, and unknown provider states fail closed.

## Production ownership

The Capacity Controller is the only production component allowed to decide project capacity.
At the start of every reconciliation it lists the project's Container Groups and requires every
remote group to be declared in `deploy/salad/services.json`. Unknown groups are treated as control-
plane drift and fail reconciliation, preventing unaccounted GPU usage from bypassing the project
budget. It calculates demand from all active `gpu.jobs`, protects running instances with deletion
cost, and
stops a group after aggregate demand reaches zero. Zero-demand shutdown has its own stability window,
`SALAD_AUTOSCALER_ZERO_DEMAND_STABLE_POLLS` (default 1), while partial downscales continue to use
`SALAD_AUTOSCALER_DOWNSCALE_STABLE_POLLS` (default 2). Fast zero-demand handling is used only when
the service has no remaining pending, retryable or running work; a target of zero caused by project
quota pressure does not use the fast path. A stopped group is treated as zero effective
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

The former video-wide runner, including its periodic controller-health guard,
was retired together with the old planning bots. The global Capacity Controller
continues to own all shared Salad replica changes for independently submitted
Postgres GPU jobs; new video-wide orchestration must restore equivalent health
monitoring before it is enabled.

## Capacity tuning

Set an explicit `SALAD_AUTOSCALER_PROJECT_MAX_REPLICAS` for the production cost/SLA budget. The
controller logs a warning when the project cap is at least the sum of all per-service maxima and
therefore provides no additional project-wide constraint.

The global cold-start default is `SALAD_AUTOSCALER_COLD_START_SECONDS`. Large services can override
it independently. LTX I2V and avatar A2V are separate workloads after task isolation and therefore
have separate calibration keys:

```text
SALAD_AUTOSCALER_QWEN_IMAGE_21_COLD_START_SECONDS=...
SALAD_AUTOSCALER_LTX25_I2V_COLD_START_SECONDS=...
SALAD_AUTOSCALER_LTX25_COLD_START_SECONDS=...
SALAD_AUTOSCALER_LTX25_I2V_RUNTIME_SECONDS=...
SALAD_AUTOSCALER_LTX25_RUNTIME_SECONDS=...
SALAD_AUTOSCALER_LTX25_I2V_TARGET_DRAIN_MINUTES=60
SALAD_AUTOSCALER_LTX25_TARGET_DRAIN_MINUTES=60
```

LTX I2V y A2V usan demanda temporal global: trabajos pendientes multiplicados
por su runtime histórico (percentil configurado), más el trabajo restante de
clips activos. Para cada pool, el target es aproximadamente
`ceil(remaining_gpu_seconds / (drain_target_seconds * utilization))`, con
límite por clips activos, réplicas ocupadas, máximo del servicio y cuota total
del proyecto. Ya no se utiliza la regla antigua de 30 clips por réplica.
El autoscaler por defecto *todavía ignora el arranque de nuevas GPUs en LTX*.

Existe una política coste/latencia opt-in, descrita en
`docs/operations/ltx-cost-aware-planner.md`, que compara explícitamente ETA,
workers calientes, arranque y coste proyectado antes de añadir réplicas.
No debe activarse sin calibrar los tiempos y precios sobre ejecuciones reales.

Los workers LTX consumen una cola Postgres compartida; el controlador no asigna
bloques de clips a instancias concretas. Durante un downscale puede marcar una
instancia ocupada como `draining`: esa GPU termina el clip que ya está
ejecutando, el fence de `gpu.capacity_drains` impide que reclame otro, y solo
una reconciliación posterior solicita el resize cuando la instancia ya está
idle. Si la demanda rebota antes de que exista un cambio de proveedor pendiente,
el drain se cancela y la instancia vuelve a ser elegible.

The controlled `i2v-static-camera-001` run recorded 3089.8 seconds of bootstrap, which demonstrates
why the generic 180-second cold-start default is not a safe production estimate for LTX I2V. Treat
that value as historical evidence, not a permanent constant: rebuilds, Salad nodes and network paths
can change it. Calibrate I2V and A2V from measured allocation, image pull, Hugging Face bootstrap,
pipeline load, readiness and inference timings on the deployed image.
