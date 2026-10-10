BEGIN;

-- Preserve historical GPU runtime observations for time-aware capacity
-- forecasting while allowing completed R2 artifacts to be reclaimed.
ALTER TABLE gpu.jobs
    ADD COLUMN IF NOT EXISTS r2_pruned_at timestamptz;

COMMENT ON COLUMN gpu.jobs.r2_pruned_at IS
    'Completed/terminal GPU artifact bundle was pruned after verified final video publication. '
    'Re-submission of the immutable job resets the queue entry for safe regeneration.';

COMMIT;
