-- V028: separate application policy prompts from exact Agent instructions.
BEGIN;

ALTER TABLE full_view_agent.prompt_templates
    ADD COLUMN IF NOT EXISTS prompt_layer TEXT NOT NULL DEFAULT 'application';

ALTER TABLE full_view_agent.prompt_templates
    DROP CONSTRAINT IF EXISTS ck_prompt_templates_layer;
ALTER TABLE full_view_agent.prompt_templates
    ADD CONSTRAINT ck_prompt_templates_layer
    CHECK (prompt_layer IN ('application', 'agent'));

-- Explicit legacy strategy: a prompt referenced by an Agent version/release was
-- being used as an Agent instruction even though the old schema could not say so.
-- Classify those exact refs as Agent prompts instead of silently treating them as
-- application policy after the migration.
UPDATE full_view_agent.prompt_templates AS prompt
SET prompt_layer = 'agent'
WHERE EXISTS (
    SELECT 1
    FROM full_view_agent.agent_versions AS version
    WHERE version.data_json ->> 'prompt_ref' =
          prompt.prompt_id || '@' || prompt.version
)
OR EXISTS (
    SELECT 1
    FROM full_view_agent.agent_release_snapshots AS release
    WHERE release.data_json ->> 'prompt_ref' =
          prompt.prompt_id || '@' || prompt.version
);

DROP INDEX IF EXISTS full_view_agent.uq_prompt_published_per_app;
CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_published_application_per_app
    ON full_view_agent.prompt_templates (app_id)
    WHERE status = 'published' AND prompt_layer = 'application';

INSERT INTO full_view_agent.schema_version (version) VALUES (28)
ON CONFLICT DO NOTHING;

COMMIT;
