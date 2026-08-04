-- Migration V006: durable analysis outcomes, reauthentication and synthetic terminals.

BEGIN;

ALTER TABLE full_view_agent.analysis_step_ledger
    ADD COLUMN IF NOT EXISTS result_status TEXT,
    ADD COLUMN IF NOT EXISTS reason_code TEXT;

UPDATE full_view_agent.analysis_step_ledger
SET result_status = 'success', reason_code = 'STEP_MIGRATED'
WHERE status = 'persisted' AND result_status IS NULL;

UPDATE full_view_agent.analysis_step_ledger
SET result_status = 'failed', reason_code = 'STEP_MIGRATED_FAILED'
WHERE status = 'failed' AND result_status IS NULL;

ALTER TABLE full_view_agent.analysis_step_ledger
    DROP CONSTRAINT IF EXISTS analysis_step_ledger_status_check,
    DROP CONSTRAINT IF EXISTS analysis_step_ledger_check,
    DROP CONSTRAINT IF EXISTS analysis_step_ledger_status_check_v6,
    DROP CONSTRAINT IF EXISTS analysis_step_ledger_references_check_v6;

ALTER TABLE full_view_agent.analysis_step_ledger
    ADD CONSTRAINT analysis_step_ledger_status_check_v6 CHECK (
        status IN (
            'reserved', 'executing', 'waiting_reauth', 'observed', 'persisted',
            'synthetic', 'indeterminate', 'failed'
        )
    ),
    ADD CONSTRAINT analysis_step_ledger_references_check_v6 CHECK (
        (status = 'persisted' AND result_status IN ('success', 'partial')
            AND reason_code IS NOT NULL AND result_id IS NOT NULL
            AND evidence_ids <> '[]')
        OR
        (status = 'observed' AND result_status IN ('success', 'partial')
            AND reason_code IS NOT NULL AND result_id IS NULL
            AND evidence_ids = '[]')
        OR
        (status = 'failed' AND result_status IN ('denied', 'failed')
            AND reason_code IS NOT NULL AND result_id IS NULL
            AND evidence_ids = '[]')
        OR
        (status = 'synthetic' AND result_status IN ('timeout', 'skipped')
            AND reason_code IS NOT NULL AND result_id IS NULL
            AND evidence_ids = '[]')
        OR
        (status IN ('reserved', 'executing', 'waiting_reauth', 'indeterminate')
            AND result_status IS NULL AND reason_code IS NULL
            AND result_id IS NULL AND evidence_ids = '[]')
    );

INSERT INTO full_view_agent.schema_version (version)
SELECT 6
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 6
);

COMMIT;
