-- V022: expose the built-in, application-scoped knowledge search capability.
-- The adapter executes inside agent-runtime; connector metadata is descriptive
-- and is never used for an outbound HTTP request.

BEGIN;

INSERT INTO full_view_agent.capability_tools (
    capability_id, name, domain, owner, version, status,
    risk_level, required_permissions, dataset_ids, description,
    connector_ref, http_method, resource_path,
    input_schema, output_schema, parameter_mapping, result_mapping,
    result_kind, data_schema_ref, max_result_rows,
    cache_enabled, cache_ttl_seconds,
    created_by, updated_by
) VALUES (
    'knowledge.search',
    '检索应用知识库',
    'knowledge',
    'agent-platform-team',
    '1.0.0',
    'published',
    'low',
    ARRAY['knowledge.search'],
    ARRAY['knowledge'],
    '检索当前用户在当前应用中获权且已发布的知识库，返回带文档、版本、切片和段落定位的引用。',
    'agent-runtime-knowledge',
    'POST',
    '/internal/knowledge/search',
    '{
      "type": "object",
      "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 4000},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}
      },
      "required": ["query"]
    }'::jsonb,
    '{}'::jsonb,
    '{}'::jsonb,
    '{}'::jsonb,
    'table',
    'schema://data/table-data-result/1.0.0',
    20,
    false, 0,
    'system-seed', 'system-seed'
) ON CONFLICT (capability_id, version) DO NOTHING;

INSERT INTO full_view_agent.application_capability_bindings (
    app_id, capability_id, capability_version, enabled,
    changed_by, reason
) VALUES (
    'full_information_view', 'knowledge.search', '1.0.0', true,
    'system-seed', 'V022 seed'
) ON CONFLICT (app_id, capability_id, capability_version) DO NOTHING;

INSERT INTO full_view_agent.schema_version (version)
SELECT 22
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 22
);

COMMIT;
