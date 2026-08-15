-- V027: immutable population Tool semantic-contract version.
BEGIN;

ALTER TABLE full_view_agent.capability_tools
    ADD COLUMN IF NOT EXISTS semantic_contract JSONB;

WITH dimensions(dimension_id, label, scope_level, list_schema, list_fields) AS (
    VALUES
        ('district', '区县', 4, 'schema://data/population-ranking-table/1.0.0', '["rank","area_code","area_name","person_count"]'::jsonb),
        ('descendant_street', '下辖街道', 4, 'schema://data/population-ranking-table/1.0.0', '["rank","area_code","area_name","person_count"]'::jsonb),
        ('descendant_community', '下辖社区', 4, 'schema://data/population-ranking-table/1.0.0', '["rank","area_code","area_name","person_count"]'::jsonb),
        ('street', '街道', 6, 'schema://data/population-metric-table/1.0.0', '["area_code","area_name","person_count"]'::jsonb),
        ('community', '社区', 9, 'schema://data/population-metric-table/1.0.0', '["area_code","area_name","person_count"]'::jsonb),
        ('grid', '网格', 12, 'schema://data/population-metric-table/1.0.0', '["area_code","area_name","person_count"]'::jsonb)
), operator_outputs(operator, output_form) AS (
    VALUES
        ('list', 'table'), ('list', 'choropleth'),
        ('sum', 'table'), ('avg', 'table'), ('min', 'table'), ('max', 'table'),
        ('top', 'table'), ('bottom', 'table'), ('rank', 'table')
), shape_rows AS (
    SELECT jsonb_build_object(
        'shape_id', 'population_' || dimension_id || '_' || operator || '_' || output_form,
        'metric_selection', jsonb_build_array('person_count'),
        'dimension_selection', jsonb_build_array(dimension_id),
        'operator_selection', jsonb_build_array(operator),
        'scope_levels', jsonb_build_array(scope_level),
        'allowed_filters', jsonb_build_array('person_category'),
        'output_forms', jsonb_build_array(output_form),
        'completeness', jsonb_build_object(
            'mode', 'complete',
            'statement', '聚合与排名仅基于完整上游区划行集。'
        ),
        'result_schema_ref', CASE
            WHEN operator IN ('sum','avg','min','max')
                THEN 'schema://data/population-aggregate-table/1.0.0'
            WHEN operator IN ('top','bottom','rank')
                THEN 'schema://data/population-ranking-table/1.0.0'
            ELSE list_schema
        END,
        'result_row_fields', CASE
            WHEN operator IN ('sum','avg','min','max')
                THEN '["operator","metric","value","area_count","completeness"]'::jsonb
            WHEN operator IN ('top','bottom','rank')
                THEN '["rank","area_code","area_name","person_count"]'::jsonb
            ELSE list_fields
        END,
        'result_fingerprint_domain', CASE
            WHEN operator IN ('sum','avg','min','max')
                THEN 'data-result:population-aggregate-table:1.0.0'
            WHEN operator IN ('top','bottom','rank') OR list_schema LIKE '%ranking%'
                THEN 'data-result:population-ranking-table:1.0.0'
            ELSE 'data-result:population-metric-table:1.0.0'
        END
    ) AS shape
    FROM dimensions CROSS JOIN operator_outputs
), contract AS (
    SELECT jsonb_build_object(
        'schema_version', '1.0',
        'subject', 'population',
        'metrics', '[{"metric_id":"person_count","label":"人口数","unit":"人","value_type":"integer"}]'::jsonb,
        'dimensions', (SELECT jsonb_agg(jsonb_build_object(
            'dimension_id', dimension_id, 'label', label,
            'kind', 'administrative_area') ORDER BY dimension_id) FROM dimensions),
        'filters', '[{"field":"person_category","label":"人口类别","operators":["eq"],"allowed_values":["solitary_elderly"]}]'::jsonb,
        'operators', '["list","sum","avg","min","max","top","bottom","rank"]'::jsonb,
        'sort', '{"allowed_fields":["person_count","district","descendant_street","street","descendant_community","community","grid"],"default_direction":"desc","tie_policy":"include_all","tie_breakers":[]}'::jsonb,
        'completeness', '{"mode":"complete","statement":"聚合与排名仅基于完整上游区划行集。"}'::jsonb,
        'output_forms', '["table","choropleth"]'::jsonb,
        'examples', '[{"question":"哪个街道人口最多？","operator":"top","metric":"person_count","dimension":"descendant_street"}]'::jsonb,
        'limitations', '["不提供个人明细","不支持合同 query_shapes 之外的组合"]'::jsonb,
        'query_shapes', (SELECT jsonb_agg(shape ORDER BY shape->>'shape_id') FROM shape_rows)
    ) AS value
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
    source.capability_id, source.name, source.domain, source.owner, '1.1.0',
    'published', source.risk_level, source.required_permissions, source.dataset_ids,
    source.description, source.connector_ref, source.http_method, source.resource_path,
    source.input_schema, source.output_schema, source.parameter_mapping,
    source.result_mapping, source.result_kind, source.data_schema_ref,
    source.timeout_ms, source.max_attempts, source.max_result_rows,
    source.cache_enabled, source.cache_ttl_seconds, source.credential_ref,
    'system-migration', 'system-migration', contract.value
FROM full_view_agent.capability_tools AS source CROSS JOIN contract
WHERE source.capability_id = 'governance.query_population_metrics'
  AND source.version = '1.0.0'
ON CONFLICT (capability_id, version) DO NOTHING;

INSERT INTO full_view_agent.application_capability_bindings (
    app_id, capability_id, capability_version, enabled
)
SELECT app_id, 'governance.query_population_metrics', '1.1.0', true
FROM full_view_agent.agent_applications
WHERE app_id = 'full_information_view'
ON CONFLICT (app_id, capability_id, capability_version)
DO UPDATE SET enabled = true;

UPDATE full_view_agent.application_capability_bindings
SET enabled = false
WHERE app_id = 'full_information_view'
  AND capability_id = 'governance.query_population_metrics'
  AND capability_version <> '1.1.0';

UPDATE full_view_agent.capability_snapshots
SET is_active = false
WHERE capability_id = 'governance.query_population_metrics';

INSERT INTO full_view_agent.capability_snapshots (
    snapshot_id, capability_id, capability_type, version,
    published_at, published_by, content, is_active
)
SELECT
    'snap_v027_population_1_1_0', capability_id, 'tool', version,
    now(), 'system-migration', to_jsonb(tool_row), true
FROM full_view_agent.capability_tools AS tool_row
WHERE capability_id = 'governance.query_population_metrics'
  AND version = '1.1.0'
ON CONFLICT (snapshot_id) DO NOTHING;

INSERT INTO full_view_agent.schema_version (version)
SELECT 27
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 27
);

COMMIT;
