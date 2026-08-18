-- V032: bind reasoning verification to an explicit model parameter profile.

ALTER TABLE full_view_agent.model_test_records
    ADD COLUMN IF NOT EXISTS profile TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'model_test_records_profile'
          AND conrelid = 'full_view_agent.model_test_records'::regclass
    ) THEN
        ALTER TABLE full_view_agent.model_test_records
            ADD CONSTRAINT model_test_records_profile CHECK (
                profile IS NULL OR profile IN ('fast', 'standard', 'deep')
            );
    END IF;
END $$;

ALTER TABLE full_view_agent.run_model_config_snapshots
    ADD COLUMN IF NOT EXISTS provider_type TEXT NOT NULL
        DEFAULT 'openai_compatible',
    ADD COLUMN IF NOT EXISTS parameter_profiles JSONB NOT NULL DEFAULT '{}';

INSERT INTO full_view_agent.schema_version (version) VALUES (32)
ON CONFLICT DO NOTHING;
