-- V031: complete, versioned model resource centre.

ALTER TABLE full_view_agent.model_configs
    ADD COLUMN IF NOT EXISTS provider_type TEXT NOT NULL DEFAULT 'openai_compatible',
    ADD COLUMN IF NOT EXISTS lifecycle TEXT NOT NULL DEFAULT 'draft',
    ADD COLUMN IF NOT EXISTS capabilities JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS parameter_profiles JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS legacy_default BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS updated_by TEXT NOT NULL DEFAULT 'system',
    ADD COLUMN IF NOT EXISTS etag INTEGER NOT NULL DEFAULT 1;

UPDATE full_view_agent.model_configs
   SET lifecycle = CASE WHEN is_enabled THEN 'published' ELSE 'draft' END
 WHERE lifecycle = 'draft';

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_configs_one_legacy_default
    ON full_view_agent.model_configs (legacy_default)
    WHERE legacy_default = true;

CREATE TABLE IF NOT EXISTS full_view_agent.model_config_versions (
    config_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    lifecycle TEXT NOT NULL,
    config JSONB NOT NULL,
    api_key_ciphertext BYTEA NOT NULL,
    api_key_nonce BYTEA NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (config_id, version),
    CONSTRAINT model_config_versions_lifecycle CHECK (
        lifecycle IN ('draft', 'tested', 'published', 'disabled')
    )
);

CREATE TABLE IF NOT EXISTS full_view_agent.model_test_records (
    test_id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    profile TEXT,
    success BOOLEAN NOT NULL,
    latency_ms INTEGER,
    actual_parameters JSONB NOT NULL DEFAULT '{}',
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    reasoning_tokens INTEGER,
    error_code TEXT,
    error_message TEXT,
    tested_by TEXT NOT NULL,
    tested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT model_test_records_kind CHECK (
        kind IN ('connection', 'chat', 'tool_calling', 'structured_output', 'reasoning')
    ),
    CONSTRAINT model_test_records_profile CHECK (
        profile IS NULL OR profile IN ('fast', 'standard', 'deep')
    )
);
CREATE INDEX IF NOT EXISTS idx_model_tests_config_version
    ON full_view_agent.model_test_records (config_id, version, tested_at DESC);

CREATE TABLE IF NOT EXISTS full_view_agent.model_audit_events (
    event_id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    previous_etag INTEGER,
    new_etag INTEGER NOT NULL,
    changed_fields JSONB NOT NULL DEFAULT '[]',
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_model_audit_config
    ON full_view_agent.model_audit_events (config_id, changed_at DESC);

CREATE TABLE IF NOT EXISTS full_view_agent.model_legacy_default (
    singleton_id TEXT PRIMARY KEY DEFAULT 'legacy',
    config_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT model_legacy_default_singleton CHECK (singleton_id = 'legacy')
);

INSERT INTO full_view_agent.schema_version (version) VALUES (31)
ON CONFLICT DO NOTHING;
