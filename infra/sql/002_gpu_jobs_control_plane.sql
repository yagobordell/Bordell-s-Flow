BEGIN;

ALTER TABLE gpu.jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE gpu.jobs DROP CONSTRAINT IF EXISTS gpu_jobs_status_check;

ALTER TABLE gpu.jobs
    ADD CONSTRAINT gpu_jobs_status_check
    CHECK (
        status IN (
            'pending',
            'running',
            'retryable_failed',
            'failed',
            'cancelled',
            'succeeded'
        )
    );

CREATE INDEX IF NOT EXISTS gpu_jobs_task_status_lease_idx
    ON gpu.jobs (task, status, lease_expires_at, created_at);

COMMENT ON TABLE gpu.jobs IS
    'Canonical application queue and transactional state for interruptible GPU jobs. '
    'Salad supplies compute capacity but is not the job-state authority.';

COMMIT;
