CREATE TABLE IF NOT EXISTS full_view_agent.prompt_templates (
    prompt_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    etag INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (prompt_id, version),
    CHECK (status IN ('draft','testing','pending_approval','published','disabled')),
    CHECK (etag > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_published_per_app
    ON full_view_agent.prompt_templates (app_id)
    WHERE status = 'published';

CREATE TABLE IF NOT EXISTS full_view_agent.prompt_lifecycle_events (
    event_id TEXT PRIMARY KEY,
    prompt_id TEXT NOT NULL,
    version TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_prompt_events_prompt
    ON full_view_agent.prompt_lifecycle_events (prompt_id, changed_at DESC);

INSERT INTO full_view_agent.schema_version (version) VALUES (21)
ON CONFLICT DO NOTHING;
