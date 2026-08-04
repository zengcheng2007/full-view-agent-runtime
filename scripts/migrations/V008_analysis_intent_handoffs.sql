-- Migration V008: durable execution ownership for natural-language analysis handoff.
-- Portable scalar/TEXT columns only; structured payloads are contract-validated JSON text.

BEGIN;

CREATE SCHEMA IF NOT EXISTS full_view_agent;

CREATE TABLE IF NOT EXISTS full_view_agent.analysis_intent_handoffs (
    handoff_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT PRIMARY KEY,
    intent_json TEXT NOT NULL,
    intent_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'captured', 'waiting_clarification', 'waiting_reauth', 'compiling',
        'compiled', 'executing', 'completed', 'partial', 'failed', 'denied',
        'cancelled'
    )),
    clarification_json TEXT NOT NULL,
    selected_area_code TEXT,
    plan_id TEXT,
    request_id TEXT,
    failure_code TEXT,
    report_result_id TEXT,
    version BIGINT NOT NULL CHECK (version > 0),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CHECK ((plan_id IS NULL) = (request_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_fva_intent_handoffs_owner
    ON full_view_agent.analysis_intent_handoffs(tenant_id, user_id, session_id);

CREATE INDEX IF NOT EXISTS idx_fva_intent_handoffs_status
    ON full_view_agent.analysis_intent_handoffs(status, updated_at);

INSERT INTO full_view_agent.schema_version (version)
SELECT 8
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 8
);

COMMIT;
