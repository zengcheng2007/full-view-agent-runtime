-- Migration V005: crash-safe analysis step authority ledger.

BEGIN;

CREATE SCHEMA IF NOT EXISTS full_view_agent;

CREATE TABLE IF NOT EXISTS full_view_agent.analysis_step_ledger (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    invocation_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('reserved', 'executing', 'persisted', 'indeterminate', 'failed')
    ),
    result_id TEXT,
    evidence_ids TEXT NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    PRIMARY KEY (run_id, step_id),
    CHECK (
        (status = 'persisted' AND result_id IS NOT NULL AND evidence_ids <> '[]')
        OR
        (status <> 'persisted' AND result_id IS NULL AND evidence_ids = '[]')
    )
);

CREATE INDEX IF NOT EXISTS idx_fva_analysis_step_owner
    ON full_view_agent.analysis_step_ledger(tenant_id, user_id, run_id);

CREATE INDEX IF NOT EXISTS idx_fva_analysis_step_call
    ON full_view_agent.analysis_step_ledger(tool_call_id);

INSERT INTO full_view_agent.schema_version (version)
SELECT 5
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 5
);

COMMIT;
