BEGIN;

ALTER TABLE gpu.jobs
    ADD COLUMN IF NOT EXISTS run_id text,
    ADD COLUMN IF NOT EXISTS run_job_count integer;

ALTER TABLE gpu.jobs
    DROP CONSTRAINT IF EXISTS gpu_jobs_run_job_count_check;
ALTER TABLE gpu.jobs
    ADD CONSTRAINT gpu_jobs_run_job_count_check
    CHECK (run_job_count IS NULL OR run_job_count >= 1);

CREATE INDEX IF NOT EXISTS gpu_jobs_task_status_run_idx
    ON gpu.jobs (task, status, run_id, created_at);

COMMENT ON COLUMN gpu.jobs.run_id IS
    'Application run identity used only by capacity orchestration and never passed to model parameters.';
COMMENT ON COLUMN gpu.jobs.run_job_count IS
    'Original per-task run batch size used by cost-aware LTX burst scaling.';

COMMIT;
