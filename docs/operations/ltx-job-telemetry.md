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
