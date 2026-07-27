-- Migration V002: Product-ledger mapping for LangGraph checkpoints.
-- Framework-owned checkpoint tables are intentionally created in the separate
-- full_view_agent_langgraph schema by AsyncPostgresSaver.setup().

BEGIN;

CREATE SCHEMA IF NOT EXISTS full_view_agent;

CREATE TABLE IF NOT EXISTS full_view_agent.orchestration_checkpoint_mappings (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    thread_id VARCHAR(255) NOT NULL UNIQUE,
    checkpoint_ns TEXT NOT NULL,
    orchestrator TEXT NOT NULL CHECK (orchestrator = 'langgraph'),
    checkpoint_id TEXT,
    version BIGINT NOT NULL CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fva_checkpoint_owner
    ON full_view_agent.orchestration_checkpoint_mappings(
        owner_user_id,
        session_id
    );

COMMIT;
