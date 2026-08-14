-- Migration V014: isolate sessions by tenant, application, and user.

BEGIN;

SET search_path TO full_view_agent;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS owner_tenant_id TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS app_id TEXT NOT NULL DEFAULT 'full_information_view';

UPDATE sessions
SET owner_tenant_id = COALESCE(
        NULLIF(data_json::jsonb ->> 'owner_tenant_id', ''),
        owner_tenant_id,
        'legacy'
    ),
    app_id = COALESCE(
        NULLIF(data_json::jsonb ->> 'app_id', ''),
        app_id,
        'full_information_view'
    );

UPDATE sessions
SET data_json = jsonb_set(
        jsonb_set(
            data_json::jsonb,
            '{owner_tenant_id}',
            to_jsonb(owner_tenant_id),
            true
        ),
        '{app_id}',
        to_jsonb(app_id),
        true
    )::text;

CREATE INDEX IF NOT EXISTS idx_fva_sessions_principal_app
    ON sessions(owner_tenant_id, owner_user_id, app_id);

INSERT INTO schema_version(version) VALUES (14)
ON CONFLICT (version) DO NOTHING;

COMMIT;
