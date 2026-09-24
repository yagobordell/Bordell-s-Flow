BEGIN;

CREATE TABLE IF NOT EXISTS gpu.capacity_controller_state (
    controller_name text PRIMARY KEY,
    controller_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('starting', 'running', 'degraded', 'stopped')),
    started_at timestamptz NOT NULL DEFAULT now(),
    last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
    last_reconcile_at timestamptz,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE gpu.capacity_controller_state IS
    'Singleton health record for the Postgres-elected Salad capacity controller leader.';
COMMENT ON COLUMN gpu.capacity_controller_state.controller_id IS
    'Process-unique identifier of the controller currently holding the advisory lock.';
COMMENT ON COLUMN gpu.capacity_controller_state.last_reconcile_at IS
    'Time of the latest successful full Salad capacity reconciliation.';

CREATE TABLE IF NOT EXISTS gpu.capacity_drains (
    service text NOT NULL,
    instance_id text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (service, instance_id)
);

CREATE INDEX IF NOT EXISTS capacity_drains_expires_idx
    ON gpu.capacity_drains (expires_at);

COMMENT ON TABLE gpu.capacity_drains IS
    'Short-lived drain intents that stop selected Salad instances from claiming new jobs.';

COMMIT;
