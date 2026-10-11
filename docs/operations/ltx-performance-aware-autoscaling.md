# LTX instance performance, guarded reallocation and run accounting

This describes the incremental, default-safe implementation introduced in PR #329.

## Root-cause status

In video02, completed I2V observations from two Salad instances had warm
inference medians near 1.70 and 14.40 s/frame for comparable frame counts.
Diffusion iterations did **not** show the same 8.8x slowdown. This is evidence
that some time is outside measured diffusion, but **not** proof of PCIe,
CPU-offload or hardware failure.

I2V workers now retain sanitized phase residuals
(stage1_nondiffusion_seconds and stage2_nondiffusion_seconds) where both
stage-total and diffusion timing exist. The residual **includes all unmeasured
nested work**: it is not a direct CPU-to-GPU transfer measurement. They also
record process CPU time, Linux host available RAM/swap, process peak RSS and
current/peak allocated/reserved CUDA memory. All metrics are best-effort and
may be absent. No exact PCIe bandwidth figures are claimed; quality settings,
CUDA graph behavior, weights and prompts are unchanged.

## Database rollout

Before deploying code that writes additional observations, run on the
operator-approved Postgres environment:

    psql "$POSTGRES_DSN" -v ON_ERROR_STOP=1 \
      -f infra/sql/010_ltx_resource_and_instance_health.sql

The migration is additive: existing observations and their primary keys are
unchanged, and new fields are nullable or have defaults. New tables are
gpu.ltx_instance_health (observations) and gpu.ltx_instance_reallocations
(provider action intents). It does not modify gpu.jobs, R2, or fingerprints.

Rollback: first disable reallocation with
SALAD_AUTOSCALER_LTX_HEALTH_MODE=off, deploy previous worker/controller
images, and **retain the additive tables/columns** until any protected
drains/reallocation requests have been resolved with Salad. Reverting source
without removing the migration is the safest rollback; deleting durable
metric tables destroys valuable evidence and is not recommended.

## Mode and policy

Set SALAD_AUTOSCALER_LTX_HEALTH_MODE in the controller environment:

* off — do not observe or use an instance-health remediation mode.
* observe — **default**: classify and persist health; no provider writes.
* recommend — also persist review_safe_drain recommendations; no provider writes.
* replace — **explicit opt-in only**: allow guarded instance reallocation.

The detector takes up to 1,500 recent observations per LTX stage, restricted
to the last seven days. It ignores unknown instance identities, cold/unproven
pipelines, retries, stale, duplicated, nonpositive or incomplete timings.
It matches exact stage, generation profile, internal width/height and frames;
requires at least five matched completed jobs and at least three peer samples.
It records states insufficient_data, healthy, suspected_slow (>2x) and
confirmed_degraded (>=3x). These thresholds are conservative defaults for
initial rollout, not empirically calibrated guarantees.

Predictions prefer >=3 warm, trusted, same-profile, same-padded-geometry,
same-frame successful samples, otherwise >=5 samples within a 2x frame
range with per-frame scaling; otherwise the existing stage historical
percentile. Instances confirmed degraded are excluded from the new warm
forecast. Queue work now sums each predicted job time rather than multiplying
one global percentile by every pending job. Project quotas and the existing
hysteresis still apply. The 60-minute target is aspirational.

## Safeguards for opt-in replace

Only the **existing singleton Capacity Controller** can initiate replacement.
It never increases the number of Salad replicas for a reallocation. The
provider API operation is targeted:

    POST .../instances/{instance_id}/reallocate

Salad documents this operation as moving the instance to another node.
It is **not** a generic container-group restart or downscale.

A confirmation-eligible candidate must be a stable Salad instance, measured
as confirmed_degraded from exact warm peers, and mapped unambiguously to a
worker. The controller first writes a protected gpu.capacity_drains fence
atomically with a durable reallocation intent. New queue claims on that
instance are fenced, and any already running LTX job finishes. On a later
poll after the grace interval, the controller verifies the instance has no
active lease and the group is stable, then transitions the intent to
requested **before** calling Salad. Ambiguous network responses never
automatically cause a duplicate request. Recovered status and fence removal
require the original instance to disappear from Salad inventory or a
reliable machine_id change. Unconfirmed requests after 7,200 seconds move
to manual_review, staying fenced until operator reconciliation.

If the API or inventory is inconsistent, the controller defers replacement;
a new physical machine is not guaranteed until a changed machine_id or
instance removal is actually observed. Existing quota, pending provider
transition and ordinary draining protection remain in effect.

**Do not enable replace during initial rollout.** Validate monitor-only
results and threshold stability against new production clips first.

## B2.2 and run sessions

B2.2 now prints start/done/failure for bounded groups with elapsed wall time,
cache hit, shots adapted, attempts and cumulative progress in:

    video/b22/state/progress.json

Only sanitized API usage and response IDs (not model prompts, image payloads
or output contents) are persisted under video/b22/usage/*.json. The B2.2
stage is included in the estimated API ledger if the model's pricing is
verified. Cache hits produce no additional API call or billable entry.

For --videos-only, run_report.json gains backward-compatible
execution_sessions with a legacy/unattributed section (when previous timings
exist), explicit session start/finish, stage durations and interrupted sessions
whose elapsed duration remains unknown. Existing runs do not silently acquire
fictional invoice totals. A billing snapshot is not a per-session invoice.

## Initial acceptance and deployment order

1. Apply migration 010 against a tested Postgres backup; run the full CI
   migrations and integration tests before production.
2. Review/push immutable Docker image digests for any updated worker code.
   Merging source code does **not** deploy Salad workers.
3. Restart the singleton Capacity Controller with health mode observe.
   Do not run two controllers or patch Salad replicas from video jobs.
4. Confirm telemetry, predictions, job fingerprints and B2.2 reports on the
   next natural video (no paid test generation is required for rollout).
5. Use recommend mode for several varied videos before **separately**
   authorizing replace. Keep the economic planner off.

Diagnostics (no prompts or media):

    SELECT stage, instance_id, health_state, ratio_to_exact_peers,
           completed_jobs, matched_jobs, reference_instances, updated_at
    FROM gpu.ltx_instance_health
    ORDER BY updated_at DESC;

    SELECT stage, instance_id, state, original_machine_id,
           detected_at, requested_at
    FROM gpu.ltx_instance_reallocations
    ORDER BY detected_at DESC;

    SELECT stage, profile, width, height, num_frames, instance_id,
           phase_seconds, resource_metrics
    FROM gpu.ltx_job_telemetry
    ORDER BY recorded_at DESC LIMIT 100;

## Next-video validation

Record per-instance s/frame by exact frame cohort, Stage 1/Stage 2
diffusion vs residual, CPU/RAM/swap availability and CUDA allocation.
Compare total billed Salad costs from the provider with observed costs;
the same is true of each API provider. Compare forecast vs actual per job,
worker states and queue wait, GPU-hours, video wall clock and number of
remediations. Only these observations can demonstrate whether costs/time
actually improved.

Limits: current resource metrics are lightweight post-job snapshots, not
precise host-wide CPU contention or transfer profilers. The provisional
during-job progress-to-controller signal and provider-billed reconciliation
remain separate future validations rather than implied guarantees.
