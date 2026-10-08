# Automated R2 cleanup after a verified video

New pipeline runs persist a unique **run-private R2 scope** in
`output/video/r2_run_scope.txt` before generating paid assets. GPU results
and their ephemeral inputs are stored under `jobs/<scoped-job-id>/`, so one
video cannot delete another video's in-progress or cached data. The permanent
Fish reference audio (for example `voices/project-narrator-v1.wav`) is never
moved or deleted.

Once the final LTX MP4 is verified and published locally, the pipeline:

1. Revalidates its SHA-256 and its saved LTX job manifest.
2. Locks the scoped GPU jobs in Postgres and requires every job to be terminal.
3. Rejects unexpected/shared input or output keys before deleting anything.
4. Deletes only the run-private `jobs/<id>/` prefixes in R2, including inputs,
   video clips, audio/image results, metadata, and rejection sidecars.
5. Marks the authoritative jobs `r2_pruned_at` **without deleting their rows**.
   Successful inference timing data remains available to the GPU autoscaler.
6. Writes `output/video/r2_cleanup.json` as a durable, retryable receipt.

R2 cleanup failure does **not** invalidate the local verified MP4 or force
paid video regeneration. A repeated publish/YouTube-only operation retries it.
If a previously pruned immutable GPU job is requested again, Postgres
transitions that job back to `pending` instead of returning a false cache hit.

## Production database migration

**Before running GPU jobs with the updated code**, apply the idempotent
migration `infra/sql/008_video_r2_artifact_pruning.sql` to the existing
Postgres database. CI automatically applies every `infra/sql/*.sql` migration.

## Legacy video runs

Preexisting runs without the new `r2_run_scope.txt` retain their immutable
job request IDs and globally shared object keys. Their R2 cache is intentionally
**not** deleted by this new automated cleanup. It is unsafe to infer exclusive
ownership of legacy content-addressed objects; clean historical objects only
after separately verifying no active dependencies.

## Scope and retention

Local scripts, images, audio, previews, and the final MP4 are not removed.
Unrelated R2 smoke/benchmark assets are not touched. The image-upscaler
already cleans its own request-scoped runtime R2 inputs/outputs independently.
This automation does not use bucket-wide deletion or prefix sweeps on shared
legacy objects.
