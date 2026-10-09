# Durable LTX per-instance telemetry (observational)

**Status:** code ready for testing, not deployed. Does not change video quality,
inference parameters, capacity or priority. No GPU benchmarks are launched.

## Installation

Apply `infra/sql/009_ltx_job_telemetry.sql` using the existing controlled
Postgres migration procedure **before** deploying worker code. No migration is
run automatically. Rollback requires a separate operator decision: do not
drop accumulated measurements when deleting GPU cache rows.

Workers persist one row for each successfully committed LTX attempt through
`PostgresJobRepository.record_ltx_telemetry`, *after* successful job status
commit. A write failure is logged but **never** invalidates a finished MP4.
The table has **no foreign key** to `gpu.jobs` so R2/cache pruning does not
remove the measurements. Recording is idempotent per (job_id, attempt_count).
Existing completed video01 jobs cannot be backfilled automatically after the
temporary objects and queue records were pruned.

Fields: job, request fingerprint, attempt, worker/Salad instance, LTX pool,
immutable generation profile, geometry, frames, task/pipeline/denoising stages,
decoding/encoding, warm-pipeline hint and a whitelist of phase seconds. Prompts,
images, audio, output paths and provider credentials are excluded.

The `pipeline_load_seconds` field measures per-job pipeline acquisition/build,
not full container boot or model download. These remain unmeasured. Timing
overlap can occur (e.g. pipeline_call includes stage1/stage2 and prompt);
**do not sum fields to compute billed GPU seconds**. Rows measure GPU processing,
not Salad billing, preemption downtime, retries after crashed containers or
provider allocation delay.

Instance correlation: when the worker receives `SALAD_INSTANCE_ID`, telemetry
uses that provider instance ID (`instance_identity_source=salad_instance`).
If this variable is missing, it records the stable `worker_id` for the
worker process as a surrogate (`worker_process`). **A worker-process ID
does not prove physical host identity and must not be used for GPU
quarantine.** Export both the identifier and its source so comparisons
are interpreted correctly.

## Offline read-only export

On a machine with DB read permission and configured `POSTGRES_DSN`:

```powershell
uv run --locked --extra dev python scripts/salad/export_ltx_job_telemetry.py .\ltx-completed.jsonl --report .\ltx-instance-report.json
```

This exports observed rows. `compare_ltx_telemetry` requires at least 3
observations per candidate instance **with exact matching frame counts to
another instance** of the same stage, profile and geometry. It reports an
advisory `review_candidate` only; **never** automatically quarantines a
worker. Keep full trace when comparing phase splits, cold starts, and
failures. `analyze_ltx_job_metrics.py` still supports its stricter
three-instance classifier for normalized JSONL observations.

Do not activate cost-aware autoscaling until telemetry proves forecasts agree
with real jobs and global project replica quota has been tested.

## Controlled setup for joint B2.2 + LTX testing

The chatbot cannot apply this migration or deploy to Salad without the operator's
private Postgres, Docker Registry and Salad credentials. Run the checked-in
rollout script **locally** from the unified feature branch.

The script defaults to a true dry run:

```powershell
.\scripts\salad\prepare_ltx_telemetry.ps1
```

Before applying, ensure the **singleton Capacity Controller is stopped**
via the normal operator procedure, no in-flight GPU jobs remain, and both
`ltx25_i2v` and `ltx25` groups are stably stopped. The script refuses
a live or pending group; it never uses the manual controller override.

Once those conditions and local `POSTGRES_DSN`, Salad credentials, Docker
login, and `uv` are ready, explicitly apply:

```powershell
.\scripts\salad\prepare_ltx_telemetry.ps1 -Apply
```

The procedure verifies both groups are stopped **before touching Postgres**,
applies migration 009, builds and pushes the shared LTX Dockerfile only once
under an immutable commit-specific tag, pins its SHA-256 digest, prepares both
Salad groups with that digest, and verifies that they remain stopped. It does
**not** start a GPU or launch a video. A failed step may leave the SQL migration
applied or one service prepared; repeat only after inspecting remote state.
Deployments are deliberately not transactional across services.

You can also run the SQL check or migration independently:

```powershell
uv run --locked --extra dev python scripts/salad/apply_ltx_telemetry_migration.py
uv run --locked --extra dev python scripts/salad/apply_ltx_telemetry_migration.py --apply
```

When prepared, resume the normal Capacity Controller via its established
operator procedure. Do not start parallel controllers, bypass its advisory
lock, enable cost-aware autoscaling, or set nonzero GPU replicas as a smoke test.
The workers will generate new telemetry only on **future successful jobs**;
old deleted R2 job objects cannot be recovered through this migration.

Rollback, if necessary: do **not** roll back the new table; restore the
previous pinned container image while stopped using the existing guarded
Salad manager, and keep the telemetry table for offline analysis.
