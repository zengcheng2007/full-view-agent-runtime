-- V020: portable knowledge metadata, draft documents and immutable publications.
-- Retrieval is supplied through an application port; no database-specific index plugin is used.

BEGIN;

CREATE TABLE IF NOT EXISTS full_view_agent.knowledge_bases (
    tenant_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    published_version INTEGER,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    application_binding_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    public_within_app BOOLEAN NOT NULL DEFAULT TRUE,
    allowed_user_ids_json TEXT NOT NULL DEFAULT '[]',
    allowed_roles_json TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app_id, knowledge_base_id),
    CONSTRAINT knowledge_bases_published_version_check
        CHECK (published_version IS NULL OR published_version >= 1)
);

CREATE TABLE IF NOT EXISTS full_view_agent.knowledge_data_sources (
    tenant_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    data_source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app_id, knowledge_base_id, data_source_id),
    FOREIGN KEY (tenant_id, app_id, knowledge_base_id)
        REFERENCES full_view_agent.knowledge_bases
            (tenant_id, app_id, knowledge_base_id),
    CONSTRAINT knowledge_data_sources_type_check CHECK (source_type IN ('upload', 'inline')),
    CONSTRAINT knowledge_data_sources_status_check CHECK (status IN ('ready', 'deleted')),
    CONSTRAINT knowledge_data_sources_media_type_check
        CHECK (media_type IN ('text/plain', 'text/markdown'))
);

CREATE TABLE IF NOT EXISTS full_view_agent.knowledge_documents (
    tenant_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    data_source_id TEXT NOT NULL DEFAULT 'inline',
    document_id TEXT NOT NULL,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app_id, knowledge_base_id, document_id),
    FOREIGN KEY (tenant_id, app_id, knowledge_base_id)
        REFERENCES full_view_agent.knowledge_bases
            (tenant_id, app_id, knowledge_base_id),
    CONSTRAINT knowledge_documents_media_type_check
        CHECK (media_type IN ('text/plain', 'text/markdown')),
    CONSTRAINT knowledge_documents_content_check CHECK (length(content) > 0)
);

CREATE TABLE IF NOT EXISTS full_view_agent.knowledge_publications (
    tenant_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    knowledge_base_version INTEGER NOT NULL,
    document_count INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app_id, knowledge_base_id, knowledge_base_version),
    FOREIGN KEY (tenant_id, app_id, knowledge_base_id)
        REFERENCES full_view_agent.knowledge_bases
            (tenant_id, app_id, knowledge_base_id),
    CONSTRAINT knowledge_publications_version_check CHECK (knowledge_base_version >= 1),
    CONSTRAINT knowledge_publications_counts_check
        CHECK (document_count >= 0 AND chunk_count >= 0)
);

CREATE TABLE IF NOT EXISTS full_view_agent.knowledge_chunks (
    tenant_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    knowledge_base_version INTEGER NOT NULL,
    document_id TEXT NOT NULL,
    document_title TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    content TEXT NOT NULL,
    page_number INTEGER,
    paragraph_start INTEGER,
    paragraph_end INTEGER,
    PRIMARY KEY (
        tenant_id, app_id, knowledge_base_id, knowledge_base_version, chunk_id
    ),
    FOREIGN KEY (tenant_id, app_id, knowledge_base_id, knowledge_base_version)
        REFERENCES full_view_agent.knowledge_publications
            (tenant_id, app_id, knowledge_base_id, knowledge_base_version),
    CONSTRAINT knowledge_chunks_ordinal_check CHECK (ordinal >= 0),
    CONSTRAINT knowledge_chunks_page_check CHECK (page_number IS NULL OR page_number >= 1),
    CONSTRAINT knowledge_chunks_paragraph_check CHECK (
        (paragraph_start IS NULL AND paragraph_end IS NULL)
        OR (paragraph_start >= 1 AND paragraph_end >= paragraph_start)
    ),
    CONSTRAINT knowledge_chunks_content_check CHECK (length(content) > 0)
);

CREATE TABLE IF NOT EXISTS full_view_agent.knowledge_index_status (
    tenant_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    knowledge_base_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app_id, knowledge_base_id, knowledge_base_version),
    FOREIGN KEY (tenant_id, app_id, knowledge_base_id, knowledge_base_version)
        REFERENCES full_view_agent.knowledge_publications
            (tenant_id, app_id, knowledge_base_id, knowledge_base_version),
    CONSTRAINT knowledge_index_status_state_check
        CHECK (status IN ('pending', 'building', 'ready', 'failed', 'deleted')),
    CONSTRAINT knowledge_index_status_count_check CHECK (chunk_count >= 0)
);

CREATE TABLE IF NOT EXISTS full_view_agent.knowledge_audit_events (
    event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    document_id TEXT,
    knowledge_base_version INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_audit_version_check
        CHECK (knowledge_base_version IS NULL OR knowledge_base_version >= 1)
);

CREATE INDEX IF NOT EXISTS idx_fva_knowledge_audit_scope
    ON full_view_agent.knowledge_audit_events
        (tenant_id, app_id, knowledge_base_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_fva_knowledge_chunks_scope
    ON full_view_agent.knowledge_chunks
        (tenant_id, app_id, knowledge_base_id, knowledge_base_version, document_id, ordinal);

INSERT INTO full_view_agent.schema_version (version)
SELECT 20
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 20
);

COMMIT;
