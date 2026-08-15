-- V029: immutable, control-plane-owned intent routing for population Tool 1.2.
BEGIN;

WITH source AS (
    SELECT tool_row.*
    FROM full_view_agent.capability_tools AS tool_row
    WHERE capability_id = 'governance.query_population_metrics'
      AND version = '1.1.0'
), enriched AS (
    SELECT source.*,
        source.semantic_contract || jsonb_build_object(
            'intent_terms', '["人口","人数","人最多","人最少","人最高","人最低"]'::jsonb,
            'excluded_intent_terms', '["独居","空巢","年龄","性别","男性","女性","明细","姓名","电话"]'::jsonb,
            'operator_intents', '[
                {"operator":"list","terms":["分布","列表"]},
                {"operator":"sum","terms":["总数","合计"]},
                {"operator":"avg","terms":["平均","均值"]},
                {"operator":"min","terms":["最小值"]},
                {"operator":"max","terms":["最大值"]},
                {"operator":"top","terms":["最多","最高"]},
                {"operator":"bottom","terms":["最少","最低"]},
                {"operator":"rank","terms":["排名","排行"]}
            ]'::jsonb,
            'metrics', (
                SELECT jsonb_agg(metric || jsonb_build_object(
                    'intent_terms', '["人口","人数","人口数","多少"]'::jsonb
                ))
                FROM jsonb_array_elements(source.semantic_contract->'metrics') AS metric
            ),
            'dimensions', (
                SELECT jsonb_agg(dimension || jsonb_build_object(
                    'intent_terms', CASE dimension->>'dimension_id'
                        WHEN 'district' THEN '["区县","城区","哪个区"]'::jsonb
                        WHEN 'descendant_street' THEN '["街道","镇街"]'::jsonb
                        WHEN 'street' THEN '["街道","镇街"]'::jsonb
                        WHEN 'descendant_community' THEN '["社区","村社"]'::jsonb
                        WHEN 'community' THEN '["社区","村社"]'::jsonb
                        WHEN 'grid' THEN '["网格"]'::jsonb
                    END
                ) ORDER BY dimension->>'dimension_id')
                FROM jsonb_array_elements(source.semantic_contract->'dimensions') AS dimension
            ),
            'query_shapes', (
                SELECT jsonb_agg(
                    shape || jsonb_build_object(
                        'argument_template', jsonb_build_object(
                            'query', jsonb_build_object(
                                'schema_version', '1.1',
                                'metrics', '$semantic.metrics',
                                'operator', '$semantic.operator',
                                'scope', '$semantic.scope',
                                'filters', '$semantic.filters',
                                'group_by', '$semantic.group_by',
                                'order_by', CASE shape->'operator_selection'->>0
                                    WHEN 'top' THEN '[{"field":"person_count","direction":"desc"}]'::jsonb
                                    WHEN 'bottom' THEN '[{"field":"person_count","direction":"asc"}]'::jsonb
                                    WHEN 'rank' THEN '[{"field":"person_count","direction":"desc"}]'::jsonb
                                    ELSE '[]'::jsonb
                                END,
                                'limit', '$semantic.limit',
                                'presentation_hint', shape->'output_forms'->>0
                            )
                        )
                    ) ORDER BY shape->>'shape_id'
                )
                FROM jsonb_array_elements(source.semantic_contract->'query_shapes') AS shape
            )
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
    capability_id, name, domain, owner, '1.2.0', 'published', risk_level,
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
SELECT app_id, 'governance.query_population_metrics', '1.2.0', true
FROM full_view_agent.agent_applications
WHERE app_id = 'full_information_view'
ON CONFLICT (app_id, capability_id, capability_version)
DO UPDATE SET enabled = true;

UPDATE full_view_agent.application_capability_bindings
SET enabled = false
WHERE app_id = 'full_information_view'
  AND capability_id = 'governance.query_population_metrics'
  AND capability_version <> '1.2.0';

UPDATE full_view_agent.capability_snapshots
SET is_active = false
WHERE capability_id = 'governance.query_population_metrics';

INSERT INTO full_view_agent.capability_snapshots (
    snapshot_id, capability_id, capability_type, version,
    published_at, published_by, content, is_active
)
SELECT
    'snap_v029_population_1_2_0', capability_id, 'tool', version,
    now(), 'system-migration', to_jsonb(tool_row), true
FROM full_view_agent.capability_tools AS tool_row
WHERE capability_id = 'governance.query_population_metrics'
  AND version = '1.2.0'
ON CONFLICT (snapshot_id) DO NOTHING;

INSERT INTO full_view_agent.schema_version (version)
SELECT 29
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 29
);

COMMIT;
