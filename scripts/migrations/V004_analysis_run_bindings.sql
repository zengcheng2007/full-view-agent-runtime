CREATE TABLE IF NOT EXISTS full_view_agent.analysis_run_bindings (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    invocation_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    report_result_id TEXT,
    version BIGINT NOT NULL CHECK (version > 0)
);

CREATE INDEX IF NOT EXISTS idx_fva_analysis_bindings_owner
    ON full_view_agent.analysis_run_bindings(tenant_id, user_id, session_id);
