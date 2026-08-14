-- V015: explicit application and application-capability lifecycle metadata.

BEGIN;

ALTER TABLE full_view_agent.agent_applications
    ADD COLUMN IF NOT EXISTS updated_by TEXT NOT NULL DEFAULT 'system',
    ADD COLUMN IF NOT EXISTS last_reason TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS etag INTEGER NOT NULL DEFAULT 1;

ALTER TABLE full_view_agent.application_capability_bindings
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS changed_by TEXT NOT NULL DEFAULT 'system',
    ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS etag INTEGER NOT NULL DEFAULT 1;

ALTER TABLE full_view_agent.agent_applications
    DROP CONSTRAINT IF EXISTS agent_applications_etag_check;
ALTER TABLE full_view_agent.agent_applications
    ADD CONSTRAINT agent_applications_etag_check CHECK (etag >= 1);

ALTER TABLE full_view_agent.application_capability_bindings
    DROP CONSTRAINT IF EXISTS application_capability_bindings_etag_check;
ALTER TABLE full_view_agent.application_capability_bindings
    ADD CONSTRAINT application_capability_bindings_etag_check CHECK (etag >= 1);

INSERT INTO full_view_agent.schema_version (version)
SELECT 15
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 15
);

COMMIT;
