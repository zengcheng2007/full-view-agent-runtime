-- V012: Run-scoped immutable bindings for model configs and capability snapshots.
--
-- These tables let a Run keep its original model config and capability set
-- even after:
--   * the runtime default config changes,
--   * the same config_id is overwritten with new metadata or a new key,
--   * dynamic tools are published / deactivated / rolled back, or
--   * the process restarts.
--
-- Security invariants:
--   * The plaintext API key is NEVER written to disk. Only the ciphertext
--     (already AES-GCM encrypted by PostgresModelConfigKeyStore) is copied
--     into the snapshot row. The encryption key is a process-secret, so a
--     database compromise alone does not expose keys.
--   * Bindings are first-write-wins per run_id: once a Run is bound to a
--     (config_id, config_version), the binding cannot be silently replaced
--     by a subsequent resolver call.
--   * Config updates do NOT clobber existing bindings. When the same
--     config_id is overwritten with a higher version, existing Runs still
--     read the snapshot row at their bound version.

-- =============================================================================
-- run_model_bindings: Run -> (config_id, config_version) first-write-wins
-- =============================================================================
CREATE TABLE IF NOT EXISTS full_view_agent.run_model_bindings (
    run_id          TEXT        PRIMARY KEY,
    config_id       TEXT        NOT NULL,
    config_version  INTEGER     NOT NULL,
    bound_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_run_model_bindings_config
    ON full_view_agent.run_model_bindings (config_id);

-- =============================================================================
-- run_model_config_snapshots: immutable snapshot of a (config_id, version)
--
-- The row is populated the first time a Run binds to a given
-- (config_id, version). Subsequent bindings to the same tuple are
-- idempotent — the snapshot row is not overwritten. The encrypted key
-- material is copied from model_configs at snapshot time, so key
-- rotation on the source row does not affect already-bound Runs.
-- =============================================================================
CREATE TABLE IF NOT EXISTS full_view_agent.run_model_config_snapshots (
    config_id          TEXT        NOT NULL,
    config_version     INTEGER     NOT NULL,
    name               TEXT        NOT NULL,
    api_base_url       TEXT        NOT NULL,
    model_name         TEXT        NOT NULL,
    protocol           TEXT        NOT NULL DEFAULT 'openai_compatible',
    timeout_seconds    INTEGER     NOT NULL DEFAULT 60,
    max_output_tokens  INTEGER     NOT NULL DEFAULT 32000,
    max_retries        INTEGER     NOT NULL DEFAULT 1,
    api_key_ciphertext BYTEA       NOT NULL,
    api_key_nonce      BYTEA       NOT NULL,
    captured_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (config_id, config_version)
);

-- =============================================================================
-- run_capability_snapshots: per-Run immutable set of published tool versions
--
-- Stores the tool_id -> version map that was in effect when the Run
-- started. The RunCapabilitySnapshotService reads this on process
-- restart to rebuild the pinned ToolRegistry. The full registry object
-- is rebuilt in-memory from the capability_tools table plus this map,
-- so we only need to persist the (run_id, tool_id, version) triples.
-- =============================================================================
CREATE TABLE IF NOT EXISTS full_view_agent.run_capability_snapshots (
    run_id       TEXT        NOT NULL,
    capability_id TEXT       NOT NULL,
    version      TEXT        NOT NULL,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, capability_id)
);

CREATE INDEX IF NOT EXISTS idx_run_cap_snapshots_run
    ON full_view_agent.run_capability_snapshots (run_id);

INSERT INTO full_view_agent.schema_version (version) VALUES (12)
ON CONFLICT DO NOTHING;
