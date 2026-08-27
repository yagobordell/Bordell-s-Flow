BEGIN;

CREATE SCHEMA IF NOT EXISTS gpu;

CREATE TABLE IF NOT EXISTS gpu.jobs (
    job_id text PRIMARY KEY,
    request_sha256 character(64) NOT NULL,
    task text NOT NULL,
    request jsonb NOT NULL,
    output_key text NOT NULL,
    transport_job_id text,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'retryable_failed', 'succeeded')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner text,
    lease_expires_at timestamptz,
    result jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (status = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR status <> 'running'
    )
);

CREATE INDEX IF NOT EXISTS gpu_jobs_status_lease_idx
    ON gpu.jobs (status, lease_expires_at);

CREATE INDEX IF NOT EXISTS gpu_jobs_updated_at_idx
    ON gpu.jobs (updated_at DESC);

COMMENT ON SCHEMA gpu IS
    'Private application state for interruptible, idempotent GPU jobs.';
COMMENT ON TABLE gpu.jobs IS
    'Immutable request identity, worker leases and reconciled output metadata.';

REVOKE ALL ON SCHEMA gpu FROM anon, authenticated;

COMMIT;
