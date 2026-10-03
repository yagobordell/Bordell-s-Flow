-- Version-isolated GPU lanes. Apply after 005_capacity_drain_provider_hold.sql.
BEGIN;
CREATE TABLE IF NOT EXISTS gpu.worker_image_routes (
    service text NOT NULL CHECK (service IN ('ltx25', 'ltx25_i2v')),
    image text NOT NULL CHECK (image ~ '^docker[.]io/yagobordell/ai-video-factory@sha256:[0-9a-f]{64}$'),
    group_name text NOT NULL UNIQUE,
    routing_contract text NOT NULL DEFAULT 'unverified'
        CHECK (routing_contract IN ('unverified', 'v1')),
    registered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (service, image)
);
-- Legacy, un-routed jobs remain eligible only for the existing fixed workers.
CREATE INDEX IF NOT EXISTS gpu_jobs_required_worker_image_idx
ON gpu.jobs (task, (request #>> '{parameters,required_worker_image}'), status, created_at);
REVOKE ALL ON gpu.worker_image_routes FROM anon, authenticated;
COMMIT;
