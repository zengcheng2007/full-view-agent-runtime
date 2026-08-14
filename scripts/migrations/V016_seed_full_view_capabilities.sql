-- V016: Seed the first reusable full-information-view aggregate capabilities.
--
-- The runtime implementations are built-in, controlled adapters. These rows
-- make the same capabilities visible to the PostgreSQL capability registry and
-- bind them to the full-information-view application. Inserts deliberately do
-- not overwrite later administrator edits when this migration is replayed.

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
    'governance.get_governance_overview',
    '查询区域治理总览',
    'governance',
    'full-information-domain-team',
    '1.0.0',
    'published',
    'low',
    ARRAY['governance.overview.aggregate.read'],
    ARRAY['governance_overview'],
    '查询授权区域的人、房、企、事、物关联数、总数和治理覆盖率，不返回对象明细。',
    'governance-geo-qxst',
    'GET',
    '/base/getBaseTotal',
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
      "area_code": "areaCode",
      "area_name": "areaName",
      "constants": {"dataBaseType": "2"}
    }'::jsonb,
    '{}'::jsonb,
    'table',
    'schema://data/governance-overview-table/1.0.0',
    5,
    true, 60,
    'system-seed', 'system-seed'
) ON CONFLICT (capability_id, version) DO NOTHING;

INSERT INTO full_view_agent.capability_tools (
    capability_id, name, domain, owner, version, status,
    risk_level, required_permissions, dataset_ids, description,
    connector_ref, http_method, resource_path,
    input_schema, output_schema, parameter_mapping, result_mapping,
    result_kind, data_schema_ref, max_result_rows,
    cache_enabled, cache_ttl_seconds,
    created_by, updated_by
) VALUES (
    'governance.query_enterprise_metrics',
    '查询企业区划分布',
    'governance',
    'full-information-domain-team',
    '1.0.0',
    'published',
    'low',
    ARRAY['governance.enterprise.aggregate.read'],
    ARRAY['enterprise'],
    '查询授权区域内企业按直接下级区划的聚合数量，不返回企业明细。',
    'governance-geo-qxst',
    'POST',
    '/enterprise/getNextEnterprise',
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
            },
            "group_by": {
              "type": "array",
              "prefixItems": [{"const": "next_area"}],
              "minItems": 1,
              "maxItems": 1
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000}
          },
          "required": ["scope", "group_by"]
        }
      },
      "required": ["query"]
    }'::jsonb,
    '{}'::jsonb,
    '{
      "transport": "form",
      "area_code": "areaCode",
      "area_name": "areaName"
    }'::jsonb,
    '{}'::jsonb,
    'table',
    'schema://data/enterprise-metric-table/1.0.0',
    200,
    true, 60,
    'system-seed', 'system-seed'
) ON CONFLICT (capability_id, version) DO NOTHING;

INSERT INTO full_view_agent.application_capability_bindings (
    app_id, capability_id, capability_version, enabled,
    changed_by, reason
) VALUES
    (
        'full_information_view',
        'governance.get_governance_overview',
        '1.0.0', true,
        'system-seed', 'V016 seed'
    ),
    (
        'full_information_view',
        'governance.query_enterprise_metrics',
        '1.0.0', true,
        'system-seed', 'V016 seed'
    )
ON CONFLICT (app_id, capability_id, capability_version) DO NOTHING;

INSERT INTO full_view_agent.schema_version (version)
SELECT 16
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 16
);

COMMIT;
