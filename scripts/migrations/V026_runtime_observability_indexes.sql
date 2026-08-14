-- Runtime observability read projections and keyset/window indexes.

BEGIN;

SET search_path TO full_view_agent;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS obs_created_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS obs_updated_at TIMESTAMPTZ;
ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS obs_created_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS obs_status TEXT,
    ADD COLUMN IF NOT EXISTS obs_outcome TEXT,
    ADD COLUMN IF NOT EXISTS obs_mode TEXT;
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS obs_occurred_at TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION fva_project_observability_session()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.obs_created_at := (NEW.data_json::jsonb ->> 'created_at')::timestamptz;
    NEW.obs_updated_at := (NEW.data_json::jsonb ->> 'updated_at')::timestamptz;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION fva_project_observability_run()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.obs_created_at := (NEW.data_json::jsonb ->> 'created_at')::timestamptz;
    NEW.obs_status := NEW.data_json::jsonb ->> 'status';
    NEW.obs_outcome := NEW.data_json::jsonb ->> 'outcome';
    NEW.obs_mode := NEW.data_json::jsonb ->> 'mode';
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION fva_project_observability_event()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.obs_occurred_at := (NEW.data_json::jsonb ->> 'occurred_at')::timestamptz;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_fva_obs_sessions_projection ON sessions;
CREATE TRIGGER trg_fva_obs_sessions_projection
BEFORE INSERT OR UPDATE OF data_json ON sessions
FOR EACH ROW EXECUTE FUNCTION fva_project_observability_session();

DROP TRIGGER IF EXISTS trg_fva_obs_runs_projection ON runs;
CREATE TRIGGER trg_fva_obs_runs_projection
BEFORE INSERT OR UPDATE OF data_json ON runs
FOR EACH ROW EXECUTE FUNCTION fva_project_observability_run();

DROP TRIGGER IF EXISTS trg_fva_obs_events_projection ON events;
CREATE TRIGGER trg_fva_obs_events_projection
BEFORE INSERT OR UPDATE OF data_json ON events
FOR EACH ROW EXECUTE FUNCTION fva_project_observability_event();

UPDATE sessions SET data_json = data_json
WHERE obs_created_at IS NULL OR obs_updated_at IS NULL;
UPDATE runs SET data_json = data_json
WHERE obs_created_at IS NULL OR obs_status IS NULL OR obs_mode IS NULL;
UPDATE events SET data_json = data_json
WHERE obs_occurred_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_fva_obs_sessions_scope_updated
    ON sessions(owner_tenant_id, app_id, obs_updated_at DESC, session_id DESC);
CREATE INDEX IF NOT EXISTS idx_fva_obs_sessions_scope_created
    ON sessions(owner_tenant_id, app_id, obs_created_at DESC, session_id DESC);
CREATE INDEX IF NOT EXISTS idx_fva_obs_runs_scope_created
    ON runs(session_id, obs_created_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_fva_obs_runs_status
    ON runs(obs_status, obs_created_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_fva_obs_runs_outcome
    ON runs(obs_outcome, obs_created_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_fva_obs_runs_mode
    ON runs(obs_mode, obs_created_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_fva_obs_events_window
    ON events(run_id, expires_at, obs_occurred_at, sequence);

INSERT INTO schema_version(version) VALUES (26)
ON CONFLICT (version) DO NOTHING;

COMMIT;
