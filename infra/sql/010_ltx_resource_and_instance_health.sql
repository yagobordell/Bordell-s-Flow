BEGIN;

-- Roll out before deploying worker code that writes the new diagnostic fields.
-- Existing immutable measurements and job fingerprints remain untouched.
ALTER TABLE gpu.ltx_job_telemetry
    ADD COLUMN IF NOT EXISTS resource_metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS pipeline_prior_successful_generations integer;

CREATE TABLE IF NOT EXISTS gpu.ltx_instance_health (
    stage text NOT NULL CHECK (stage IN ('ltx25', 'ltx25_i2v')),
    instance_id text NOT NULL,
    health_state text NOT NULL CHECK (
        health_state IN ('healthy', 'insufficient_data', 'suspected_slow',
                         'confirmed_degraded', 'recovered')
    ),
    ratio_to_exact_peers double precision,
    completed_jobs integer NOT NULL DEFAULT 0,
    matched_jobs integer NOT NULL DEFAULT 0,
    reference_instances integer NOT NULL DEFAULT 0,
    recommendation text NOT NULL DEFAULT 'observe',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stage, instance_id)
);

COMMENT ON TABLE gpu.ltx_instance_health IS
    'Observation-only durable per-Salad-instance health; not physical host identity.';

REVOKE ALL ON TABLE gpu.ltx_instance_health FROM anon, authenticated;

COMMIT;
