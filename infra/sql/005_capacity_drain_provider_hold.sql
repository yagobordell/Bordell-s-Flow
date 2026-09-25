BEGIN;

ALTER TABLE gpu.capacity_drains
    ADD COLUMN IF NOT EXISTS hold_until_confirmed boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN gpu.capacity_drains.hold_until_confirmed IS
    'When true, the instance remains drain-fenced regardless of expires_at until the Capacity Controller explicitly clears it after Salad confirms the provider change.';

COMMIT;
