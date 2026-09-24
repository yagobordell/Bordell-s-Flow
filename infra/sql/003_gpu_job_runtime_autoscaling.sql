BEGIN;

ALTER TABLE gpu.jobs
    ADD COLUMN IF NOT EXISTS started_at timestamptz,
    ADD COLUMN IF NOT EXISTS finished_at timestamptz;

CREATE OR REPLACE FUNCTION gpu.set_job_runtime_timestamps()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'running'
       AND (
           OLD.status IS DISTINCT FROM 'running'
           OR NEW.lease_owner IS DISTINCT FROM OLD.lease_owner
       ) THEN
        NEW.started_at := now();
        NEW.finished_at := NULL;
    ELSIF OLD.status = 'running'
          AND NEW.status IN (
              'retryable_failed',
              'failed',
              'cancelled',
              'succeeded'
          ) THEN
        NEW.finished_at := now();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS gpu_jobs_runtime_timestamps ON gpu.jobs;
CREATE TRIGGER gpu_jobs_runtime_timestamps
BEFORE UPDATE OF status, lease_owner ON gpu.jobs
FOR EACH ROW
EXECUTE FUNCTION gpu.set_job_runtime_timestamps();

CREATE INDEX IF NOT EXISTS gpu_jobs_task_finished_runtime_idx
    ON gpu.jobs (task, finished_at DESC)
    WHERE status = 'succeeded'
      AND started_at IS NOT NULL
      AND finished_at IS NOT NULL;

COMMENT ON COLUMN gpu.jobs.started_at IS
    'Start time of the latest physical inference attempt; used by predictive Salad scaling.';
COMMENT ON COLUMN gpu.jobs.finished_at IS
    'Finish time of the latest physical inference attempt; used for runtime percentiles.';

COMMIT;
