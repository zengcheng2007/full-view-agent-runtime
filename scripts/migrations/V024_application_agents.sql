-- V024: application-scoped Agents, versioned assembly and immutable releases.

BEGIN;

CREATE TABLE IF NOT EXISTS full_view_agent.agent_definitions (
    app_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    data_json JSONB NOT NULL,
    PRIMARY KEY (app_id, agent_id),
    FOREIGN KEY (app_id) REFERENCES full_view_agent.agent_applications(app_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS full_view_agent.agent_versions (
    app_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    version TEXT NOT NULL,
    data_json JSONB NOT NULL,
    PRIMARY KEY (app_id, agent_id, version),
    FOREIGN KEY (app_id, agent_id)
        REFERENCES full_view_agent.agent_definitions(app_id, agent_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS full_view_agent.agent_model_policies (
    app_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    version TEXT NOT NULL,
    data_json JSONB NOT NULL,
    PRIMARY KEY (app_id, agent_id, version),
    FOREIGN KEY (app_id, agent_id, version)
        REFERENCES full_view_agent.agent_versions(app_id, agent_id, version)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS full_view_agent.agent_release_snapshots (
    release_id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    data_json JSONB NOT NULL,
    UNIQUE (app_id, agent_id, agent_version),
    FOREIGN KEY (app_id, agent_id, agent_version)
        REFERENCES full_view_agent.agent_versions(app_id, agent_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_release_active
    ON full_view_agent.agent_release_snapshots(app_id, agent_id)
    WHERE is_active;

CREATE TABLE IF NOT EXISTS full_view_agent.run_agent_release_snapshots (
    run_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    data_json JSONB NOT NULL,
    FOREIGN KEY (release_id)
        REFERENCES full_view_agent.agent_release_snapshots(release_id)
);

INSERT INTO full_view_agent.schema_version (version)
SELECT 24
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 24
);

COMMIT;
