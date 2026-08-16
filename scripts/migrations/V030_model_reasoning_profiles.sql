BEGIN;

ALTER TABLE full_view_agent.model_configs
    ADD COLUMN IF NOT EXISTS reasoning_capability JSONB NOT NULL DEFAULT '{"mode":"unsupported","fast_profile":null,"deep_profile":null}'::jsonb;

ALTER TABLE full_view_agent.run_model_config_snapshots
    ADD COLUMN IF NOT EXISTS reasoning_capability JSONB NOT NULL DEFAULT '{"mode":"unsupported","fast_profile":null,"deep_profile":null}'::jsonb;

INSERT INTO full_view_agent.schema_version (version) VALUES (30)
ON CONFLICT DO NOTHING;

COMMIT;
