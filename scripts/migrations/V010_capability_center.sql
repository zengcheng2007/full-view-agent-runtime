-- V010: Capability Center + Model Config + Connectors
-- P2-1/2/3: Tool/Skill/Workflow lifecycle, publish snapshots,
-- simplified model configuration, approved HTTP connectors.

-- =============================================================================
-- Connectors (P2-3): approved HTTP endpoints that tools can bind to
-- =============================================================================
CREATE TABLE IF NOT EXISTS full_view_agent.connectors (
    connector_id  TEXT        PRIMARY KEY,
    name          TEXT        NOT NULL,
    base_url      TEXT        NOT NULL,
    description   TEXT        NOT NULL DEFAULT '',
    allowed_path_prefixes TEXT[] NOT NULL DEFAULT '{}',
    denied_hosts  TEXT[]      NOT NULL DEFAULT '{}',
    is_active     BOOLEAN     NOT NULL DEFAULT true,
    credential_ref TEXT,
    timeout_ms    INTEGER     NOT NULL DEFAULT 8000,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT connectors_name_unique UNIQUE (name),
    CONSTRAINT connectors_base_url_check CHECK (base_url ~ '^https?://'),
    CONSTRAINT connectors_timeout_range CHECK (timeout_ms BETWEEN 100 AND 120000)
);

CREATE INDEX IF NOT EXISTS idx_connectors_is_active
    ON full_view_agent.connectors (is_active);

-- =============================================================================
-- Capability Tools (P2-1 + P2-3): read-only HTTP tool definitions
-- =============================================================================
CREATE TABLE IF NOT EXISTS full_view_agent.capability_tools (
    capability_id TEXT        NOT NULL,
    name          TEXT        NOT NULL,
    domain        TEXT        NOT NULL DEFAULT 'governance',
    owner         TEXT        NOT NULL,
    version       TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'draft',
    risk_level    TEXT        NOT NULL DEFAULT 'low',
    required_permissions TEXT[] NOT NULL DEFAULT '{}',
    dataset_ids   TEXT[]      NOT NULL DEFAULT '{}',
    description   TEXT        NOT NULL DEFAULT '',
    connector_ref TEXT        NOT NULL,
    http_method   TEXT        NOT NULL DEFAULT 'GET',
    resource_path TEXT        NOT NULL,
    input_schema  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    output_schema JSONB       NOT NULL DEFAULT '{}'::jsonb,
    parameter_mapping JSONB   NOT NULL DEFAULT '{}'::jsonb,
    result_mapping JSONB      NOT NULL DEFAULT '{}'::jsonb,
    result_kind   TEXT        NOT NULL DEFAULT 'table',
    data_schema_ref TEXT      NOT NULL DEFAULT '',
    timeout_ms    INTEGER     NOT NULL DEFAULT 8000,
    max_attempts  INTEGER     NOT NULL DEFAULT 2,
    max_result_rows INTEGER   NOT NULL DEFAULT 1000,
    cache_enabled BOOLEAN     NOT NULL DEFAULT true,
    cache_ttl_seconds INTEGER NOT NULL DEFAULT 60,
    credential_ref TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT        NOT NULL DEFAULT 'system',
    updated_by    TEXT        NOT NULL DEFAULT 'system',
    etag          INTEGER     NOT NULL DEFAULT 1,
    PRIMARY KEY (capability_id, version),
    CONSTRAINT cap_tools_status_check CHECK (status IN (
        'draft', 'testing', 'pending_approval', 'published', 'disabled'
    )),
    CONSTRAINT cap_tools_risk_check CHECK (risk_level IN ('low', 'medium', 'high')),
    CONSTRAINT cap_tools_method_check CHECK (http_method IN ('GET', 'POST')),
    CONSTRAINT cap_tools_path_check CHECK (
        resource_path LIKE '/%' AND resource_path NOT LIKE '%..%' AND resource_path NOT LIKE '%//%'
    ),
    CONSTRAINT cap_tools_timeout_range CHECK (timeout_ms BETWEEN 100 AND 120000),
    CONSTRAINT cap_tools_attempts_range CHECK (max_attempts BETWEEN 1 AND 5),
    CONSTRAINT cap_tools_rows_range CHECK (max_result_rows BETWEEN 1 AND 10000),
    CONSTRAINT cap_tools_ttl_range CHECK (cache_ttl_seconds BETWEEN 0 AND 86400)
);

CREATE INDEX IF NOT EXISTS idx_cap_tools_status
    ON full_view_agent.capability_tools (status);

-- =============================================================================
-- Capability Skills (P2-4): model usage guidance with tool whitelist
-- =============================================================================
CREATE TABLE IF NOT EXISTS full_view_agent.capability_skills (
    capability_id TEXT        NOT NULL,
    name          TEXT        NOT NULL,
    domain        TEXT        NOT NULL DEFAULT 'governance',
    owner         TEXT        NOT NULL,
    version       TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'draft',
    risk_level    TEXT        NOT NULL DEFAULT 'low',
    required_permissions TEXT[] NOT NULL DEFAULT '{}',
    dataset_ids   TEXT[]      NOT NULL DEFAULT '{}',
    description   TEXT        NOT NULL DEFAULT '',
    applicable_questions TEXT[] NOT NULL DEFAULT '{}',
    guidance      TEXT        NOT NULL DEFAULT '',
    allowed_tool_ids TEXT[]   NOT NULL DEFAULT '{}',
    input_constraints JSONB   NOT NULL DEFAULT '{}'::jsonb,
    output_constraints JSONB  NOT NULL DEFAULT '{}'::jsonb,
    examples      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    counter_examples JSONB    NOT NULL DEFAULT '[]'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT        NOT NULL DEFAULT 'system',
    updated_by    TEXT        NOT NULL DEFAULT 'system',
    etag          INTEGER     NOT NULL DEFAULT 1,
    PRIMARY KEY (capability_id, version),
    CONSTRAINT cap_skills_status_check CHECK (status IN (
        'draft', 'testing', 'pending_approval', 'published', 'disabled'
    ))
);

-- =============================================================================
-- Capability Workflows (P2-4): controlled multi-step orchestration
-- =============================================================================
CREATE TABLE IF NOT EXISTS full_view_agent.capability_workflows (
    capability_id TEXT        NOT NULL,
    name          TEXT        NOT NULL,
    domain        TEXT        NOT NULL DEFAULT 'governance',
    owner         TEXT        NOT NULL,
    version       TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'draft',
    risk_level    TEXT        NOT NULL DEFAULT 'low',
    required_permissions TEXT[] NOT NULL DEFAULT '{}',
    dataset_ids   TEXT[]      NOT NULL DEFAULT '{}',
    description   TEXT        NOT NULL DEFAULT '',
    nodes         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    edges         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    timeout_seconds INTEGER   NOT NULL DEFAULT 300,
    requires_human_confirmation BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT        NOT NULL DEFAULT 'system',
    updated_by    TEXT        NOT NULL DEFAULT 'system',
    etag          INTEGER     NOT NULL DEFAULT 1,
    PRIMARY KEY (capability_id, version),
    CONSTRAINT cap_workflows_status_check CHECK (status IN (
        'draft', 'testing', 'pending_approval', 'published', 'disabled'
    )),
    CONSTRAINT cap_workflows_timeout_range CHECK (timeout_seconds BETWEEN 10 AND 3600)
);

-- =============================================================================
-- Capability Snapshots: immutable published versions
-- =============================================================================
CREATE TABLE IF NOT EXISTS full_view_agent.capability_snapshots (
    snapshot_id    TEXT        PRIMARY KEY,
    capability_id  TEXT        NOT NULL,
    capability_type TEXT       NOT NULL,
    version        TEXT        NOT NULL,
    published_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_by   TEXT        NOT NULL,
    content        JSONB       NOT NULL,
    is_active      BOOLEAN     NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_cap_snapshots_capability
    ON full_view_agent.capability_snapshots (capability_id, is_active);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cap_snapshots_active
    ON full_view_agent.capability_snapshots (capability_id)
    WHERE is_active = true;

-- =============================================================================
-- Capability Lifecycle Events: audit trail
-- =============================================================================
CREATE TABLE IF NOT EXISTS full_view_agent.capability_lifecycle_events (
    event_id       TEXT        PRIMARY KEY,
    capability_id  TEXT        NOT NULL,
    from_status    TEXT        NOT NULL,
    to_status      TEXT        NOT NULL,
    version        TEXT        NOT NULL,
    changed_by     TEXT        NOT NULL,
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason         TEXT        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_cap_lifecycle_capability
    ON full_view_agent.capability_lifecycle_events (capability_id, changed_at DESC);

-- =============================================================================
-- Model Configs (P2-2): simplified model provider configuration
-- =============================================================================
CREATE TABLE IF NOT EXISTS full_view_agent.model_configs (
    config_id        TEXT        PRIMARY KEY,
    name             TEXT        NOT NULL,
    api_base_url     TEXT        NOT NULL,
    api_key_ciphertext BYTEA     NOT NULL,
    api_key_nonce    BYTEA       NOT NULL,
    model_name       TEXT        NOT NULL,
    protocol         TEXT        NOT NULL DEFAULT 'openai_compatible',
    timeout_seconds  INTEGER     NOT NULL DEFAULT 60,
    max_output_tokens INTEGER   NOT NULL DEFAULT 32000,
    max_retries      INTEGER     NOT NULL DEFAULT 1,
    is_enabled       BOOLEAN     NOT NULL DEFAULT false,
    notes            TEXT        NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       TEXT        NOT NULL DEFAULT 'system',
    version          INTEGER     NOT NULL DEFAULT 1,
    CONSTRAINT model_configs_name_unique UNIQUE (name),
    CONSTRAINT model_configs_max_output_tokens_range CHECK (max_output_tokens BETWEEN 100 AND 128000),
    CONSTRAINT model_configs_timeout_range CHECK (timeout_seconds BETWEEN 5 AND 600),
    CONSTRAINT model_configs_retries_range CHECK (max_retries BETWEEN 0 AND 5)
);

CREATE INDEX IF NOT EXISTS idx_model_configs_enabled
    ON full_view_agent.model_configs (is_enabled) WHERE is_enabled = true;

-- =============================================================================
-- Seed: record migration version
-- =============================================================================
INSERT INTO full_view_agent.schema_version (version) VALUES (10)
ON CONFLICT DO NOTHING;
