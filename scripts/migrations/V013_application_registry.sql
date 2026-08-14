-- Migration V013: shared application registry and application capability binding.
-- The Agent Runtime can now host peer applications instead of hard-coding the
-- full information view into admission and capability discovery.

BEGIN;

CREATE TABLE IF NOT EXISTS full_view_agent.agent_applications (
    app_id              TEXT        PRIMARY KEY,
    name                TEXT        NOT NULL,
    default_agent_id    TEXT        NOT NULL,
    identity_adapter_id TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'active',
    description         TEXT        NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT agent_applications_status_check
        CHECK (status IN ('active', 'disabled')),
    CONSTRAINT agent_applications_id_check
        CHECK (app_id ~ '^[a-z][a-z0-9_]{1,63}$')
);

CREATE TABLE IF NOT EXISTS full_view_agent.application_capability_bindings (
    app_id              TEXT        NOT NULL,
    capability_id       TEXT        NOT NULL,
    capability_version  TEXT        NOT NULL,
    enabled             BOOLEAN     NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, capability_id, capability_version),
    CONSTRAINT application_capability_bindings_app_fk
        FOREIGN KEY (app_id)
        REFERENCES full_view_agent.agent_applications(app_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_application_capability_bindings_enabled
    ON full_view_agent.application_capability_bindings (app_id, enabled);

INSERT INTO full_view_agent.agent_applications (
    app_id, name, default_agent_id, identity_adapter_id, status, description
) VALUES
    (
        'full_information_view', '全量信息视图',
        'governance_general_agent', 'identity.legacy_geo', 'active',
        '全量信息视图首个领域应用'
    ),
    (
        'unified_address', '统一地址平台',
        'unified_address_agent', 'identity.unified_address', 'disabled',
        '待身份适配器和首批地址能力就绪后启用'
    )
ON CONFLICT (app_id) DO NOTHING;

INSERT INTO full_view_agent.application_capability_bindings (
    app_id, capability_id, capability_version, enabled
)
SELECT 'full_information_view', capability_id, version, true
FROM full_view_agent.capability_tools
WHERE status = 'published'
ON CONFLICT (app_id, capability_id, capability_version) DO NOTHING;

INSERT INTO full_view_agent.schema_version (version)
SELECT 13
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 13
);

COMMIT;
