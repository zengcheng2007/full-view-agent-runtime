-- Migration V009: isolate Agent and Analysis LangGraph checkpoints for one Run.
-- Existing rows remain graph_kind=agent and retain their historical thread_id.

BEGIN;

ALTER TABLE full_view_agent.orchestration_checkpoint_mappings
    ADD COLUMN IF NOT EXISTS graph_kind TEXT NOT NULL DEFAULT 'agent';

ALTER TABLE full_view_agent.orchestration_checkpoint_mappings
    DROP CONSTRAINT IF EXISTS orchestration_checkpoint_mappings_pkey;

ALTER TABLE full_view_agent.orchestration_checkpoint_mappings
    ADD CONSTRAINT orchestration_checkpoint_mappings_pkey
    PRIMARY KEY (run_id, graph_kind);

ALTER TABLE full_view_agent.orchestration_checkpoint_mappings
    DROP CONSTRAINT IF EXISTS orchestration_checkpoint_mappings_graph_kind_check;

ALTER TABLE full_view_agent.orchestration_checkpoint_mappings
    ADD CONSTRAINT orchestration_checkpoint_mappings_graph_kind_check
    CHECK (graph_kind IN ('agent', 'analysis'));

ALTER TABLE full_view_agent.orchestration_checkpoint_mappings
    ALTER COLUMN graph_kind DROP DEFAULT;

INSERT INTO full_view_agent.schema_version (version)
SELECT 9
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 9
);

COMMIT;
