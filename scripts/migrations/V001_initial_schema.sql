-- Migration V001: Initial schema for Agent Runtime
-- Aligned with postgres_persistence.py _ddl_statements()
-- Run: psql -U <user> -d <db> -f scripts/migrations/V001_initial_schema.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS full_view_agent;

SET search_path TO full_view_agent;

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fva_runs_session ON runs(session_id);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fva_messages_session
    ON messages(session_id, created_at, message_id);

CREATE TABLE IF NOT EXISTS results (
    result_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frontend_commands (
    command_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fva_frontend_commands_run
    ON frontend_commands(run_id);

CREATE TABLE IF NOT EXISTS frontend_command_receipts (
    command_id TEXT NOT NULL,
    client_instance_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY(command_id, client_instance_id)
);

CREATE TABLE IF NOT EXISTS steers (
    steer_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_requests (
    run_id TEXT PRIMARY KEY,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_contexts (
    run_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    UNIQUE(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS idempotency_records (
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    result_type TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, scope, key)
);

CREATE TABLE IF NOT EXISTS credentials (
    credential_ref TEXT PRIMARY KEY,
    token_ciphertext BYTEA NOT NULL,
    nonce BYTEA NOT NULL,
    subject_user_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_fva_credentials_subject
    ON credentials(subject_user_id);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;

COMMIT;
