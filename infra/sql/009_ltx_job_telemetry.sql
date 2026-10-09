BEGIN;

-- Durable observations survive intentional pruning of gpu.jobs and R2 job bundles.
-- Do not add a foreign key to gpu.jobs: successful video runs remove cache rows.
CREATE TABLE IF NOT EXISTS gpu.ltx_job_telemetry (
    job_id text NOT NULL,
    request_sha256 character(64) NOT NULL,
    attempt_count integer NOT NULL CHECK (attempt_count > 0),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    instance_id text,
    worker_id text NOT NULL,
    stage text NOT NULL CHECK (stage IN ('ltx25', 'ltx25_i2v')),
    profile text NOT NULL,
    width integer NOT NULL CHECK (width > 0),
    height integer NOT NULL CHECK (height > 0),
    num_frames integer NOT NULL CHECK (num_frames > 0),
    task_seconds double precision NOT NULL CHECK (task_seconds >= 0),
    inference_seconds double precision,
    pipeline_load_seconds double precision,
    stage1_seconds double precision,
    stage2_seconds double precision,
    video_decode_seconds double precision,
    encode_seconds double precision,
    pipeline_reused boolean,
    phase_seconds jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (job_id, attempt_count)
);

CREATE INDEX IF NOT EXISTS ltx_job_telemetry_cohort_idx
    ON gpu.ltx_job_telemetry (stage, profile, width, height, num_frames);
CREATE INDEX IF NOT EXISTS ltx_job_telemetry_instance_idx
    ON gpu.ltx_job_telemetry (instance_id, recorded_at DESC);

COMMENT ON TABLE gpu.ltx_job_telemetry IS
    'Durable per-attempt LTX timings; no prompts, generated media, or billing claims.';

REVOKE ALL ON TABLE gpu.ltx_job_telemetry FROM anon, authenticated;

COMMIT;
