-- V033: immutable population Tool version that rejects undeclared median ranking.
--
-- A median-ranking request needs an explicit controller-defined shape (including
-- even-row and tie rules).  Until then the published semantic contract refuses
-- it rather than treating it as an ordinary ranking/list query.
BEGIN;

WITH source AS (
    SELECT tool_row.*
    FROM full_view_agent.capability_tools AS tool_row
    WHERE capability_id = 'governance.query_population_metrics'
      AND version = '1.2.0'
), enriched AS (
    SELECT source.*,
        source.semantic_contract || jsonb_build_object(
            'excluded_intent_terms', (
                SELECT jsonb_agg(term)
                FROM (
                    SELECT DISTINCT term
                    FROM jsonb_array_elements_text(
                        COALESCE(source.semantic_contract->'excluded_intent_terms', '[]'::jsonb)
                    ) AS item(term)
                    UNION ALL SELECT '中位数'
                    UNION ALL SELECT '中位'
                ) AS terms
            ),
            'limitations', COALESCE(source.semantic_contract->'limitations', '[]'::jsonb)
                || '["未发布中位数排名能力形态，不将其降级为普通排名或列表"]'::jsonb
        ) AS enriched_contract
    FROM source
)
INSERT INTO full_view_agent.capability_tools (
    capability_id, name, domain, owner, version, status, risk_level,
    required_permissions, dataset_ids, description, connector_ref, http_method,
    resource_path, input_schema, output_schema, parameter_mapping, result_mapping,
    result_kind, data_schema_ref, timeout_ms, max_attempts, max_result_rows,
    cache_enabled, cache_ttl_seconds, credential_ref, created_by, updated_by,
    semantic_contract
)
SELECT
    capability_id, name, domain, owner, '1.3.0', 'published', risk_level,
    required_permissions, dataset_ids, description, connector_ref, http_method,
    resource_path, input_schema, output_schema, parameter_mapping, result_mapping,
    result_kind, data_schema_ref, timeout_ms, max_attempts, max_result_rows,
    cache_enabled, cache_ttl_seconds, credential_ref,
    'system-migration', 'system-migration', enriched_contract
FROM enriched
ON CONFLICT (capability_id, version) DO NOTHING;

INSERT INTO full_view_agent.application_capability_bindings (
    app_id, capability_id, capability_version, enabled
)
SELECT app_id, 'governance.query_population_metrics', '1.3.0', true
FROM full_view_agent.agent_applications
WHERE app_id = 'full_information_view'
ON CONFLICT (app_id, capability_id, capability_version)
DO UPDATE SET enabled = true;

UPDATE full_view_agent.application_capability_bindings
SET enabled = false
WHERE app_id = 'full_information_view'
  AND capability_id = 'governance.query_population_metrics'
  AND capability_version <> '1.3.0';

UPDATE full_view_agent.capability_snapshots
SET is_active = false
WHERE capability_id = 'governance.query_population_metrics';

INSERT INTO full_view_agent.capability_snapshots (
    snapshot_id, capability_id, capability_type, version,
    published_at, published_by, content, is_active
)
SELECT
    'snap_v033_population_1_3_0', capability_id, 'tool', version,
    now(), 'system-migration', to_jsonb(tool_row), true
FROM full_view_agent.capability_tools AS tool_row
WHERE capability_id = 'governance.query_population_metrics'
  AND version = '1.3.0'
ON CONFLICT (snapshot_id) DO NOTHING;

INSERT INTO full_view_agent.schema_version (version)
SELECT 33
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 33
);

COMMIT;
