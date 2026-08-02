-- Migration V003: Trusted server-side AnalysisPlan authority.
-- Uses portable scalar/TEXT columns only; no database-specific extension.

BEGIN;

CREATE SCHEMA IF NOT EXISTS full_view_agent;

CREATE TABLE IF NOT EXISTS full_view_agent.analysis_plans (
    namespace TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    catalog_fingerprint TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fva_analysis_plans_scope
    ON full_view_agent.analysis_plans(tenant_id, user_id, run_id, plan_id);

CREATE INDEX IF NOT EXISTS idx_fva_analysis_plans_request
    ON full_view_agent.analysis_plans(tenant_id, user_id, run_id, request_id);

INSERT INTO full_view_agent.schema_version (version)
SELECT 3
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 3
);

COMMIT;
