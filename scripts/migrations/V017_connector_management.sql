-- V017: optimistic Connector management and audit trail.

ALTER TABLE full_view_agent.connectors
    ADD COLUMN IF NOT EXISTS created_by TEXT NOT NULL DEFAULT 'system',
    ADD COLUMN IF NOT EXISTS updated_by TEXT NOT NULL DEFAULT 'system',
    ADD COLUMN IF NOT EXISTS etag INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS full_view_agent.connector_audit_events (
    event_id       TEXT        PRIMARY KEY,
    connector_id   TEXT        NOT NULL
        REFERENCES full_view_agent.connectors(connector_id) ON DELETE RESTRICT,
    action         TEXT        NOT NULL,
    actor          TEXT        NOT NULL,
    reason         TEXT        NOT NULL,
    previous_etag  INTEGER     NOT NULL,
    new_etag       INTEGER     NOT NULL,
    changed_fields TEXT[]      NOT NULL DEFAULT '{}',
    from_active    BOOLEAN     NOT NULL,
    to_active      BOOLEAN     NOT NULL,
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT connector_audit_action_check
        CHECK (action IN ('update', 'enable', 'disable')),
    CONSTRAINT connector_audit_etag_check
        CHECK (previous_etag >= 1 AND new_etag = previous_etag + 1),
    CONSTRAINT connector_audit_reason_check
        CHECK (length(btrim(reason)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_connector_audit_connector
    ON full_view_agent.connector_audit_events (connector_id, changed_at DESC);

INSERT INTO full_view_agent.schema_version (version) VALUES (17)
ON CONFLICT DO NOTHING;
