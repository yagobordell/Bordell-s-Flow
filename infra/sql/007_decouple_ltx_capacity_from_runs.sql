BEGIN;

COMMENT ON COLUMN gpu.jobs.run_id IS
    'Application run correlation identity. Retained for observability and reconciliation only; never used to size or own Salad capacity.';

COMMENT ON COLUMN gpu.jobs.run_job_count IS
    'Deprecated legacy LTX run-size metadata retained for backwards compatibility with historical jobs. Ignored by current capacity orchestration.';

COMMIT;
