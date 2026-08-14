-- V025: publish and bind the aggregate-only governance-power capability.
-- Personal names, phone numbers and governance-force detail records are not
-- part of this contract. Replays preserve administrator-owned edits.

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
    'governance.query_governance_power_metrics',
    '查询治理力量汇总',
    'governance',
    'full-information-domain-team',
    '1.0.0',
    'published',
    'low',
    ARRAY['governance.power.aggregate.read'],
    ARRAY['governance_power'],
    '查询授权区域的治理力量分类汇总，只返回聚合数量，不返回人员姓名、电话或明细。',
    'governance-geo-qxst',
    'GET',
    '/api/getGovernancePower',
    '{
      "type": "object",
      "properties": {
        "query": {
          "type": "object",
          "properties": {
            "scope": {
              "type": "object",
              "properties": {
                "area_code": {"type": "string"},
                "include_descendants": {"type": "boolean", "default": true}
              },
              "required": ["area_code"]
            }
          },
          "required": ["scope"]
        }
      },
      "required": ["query"]
    }'::jsonb,
    '{}'::jsonb,
    '{
      "transport": "query",
      "area_code": "areaCodeValue",
      "area_name": "areaCodeName"
    }'::jsonb,
    '{}'::jsonb,
    'table',
    'schema://data/governance-power-metric-table/1.0.0',
    7,
    true, 60,
    'system-seed', 'system-seed'
) ON CONFLICT (capability_id, version) DO NOTHING;

INSERT INTO full_view_agent.application_capability_bindings (
    app_id, capability_id, capability_version, enabled,
    changed_by, reason
) VALUES (
    'full_information_view',
    'governance.query_governance_power_metrics',
    '1.0.0', true,
    'system-seed', 'V025 seed'
)
ON CONFLICT (app_id, capability_id, capability_version) DO NOTHING;

INSERT INTO full_view_agent.schema_version (version)
SELECT 25
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 25
);

COMMIT;
