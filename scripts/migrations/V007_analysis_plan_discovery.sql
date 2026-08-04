-- Migration V007: Efficient recovery of the latest trusted plan for a queued run.
-- Portable scalar columns only; no PostgreSQL extension or JSON operator.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_fva_analysis_plans_latest
    ON full_view_agent.analysis_plans(
        tenant_id,
        user_id,
        run_id,
        created_at DESC,
        namespace DESC
    );

INSERT INTO full_view_agent.schema_version (version)
SELECT 7
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 7
);

COMMIT;
