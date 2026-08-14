-- V023: expose the controlled industry-name shape on the existing enterprise Tool.
BEGIN;

UPDATE full_view_agent.capability_tools
SET
    description = '查询授权区域内企业聚合数量，不返回企业明细。'
        || 'group_by=[next_area] 时按直接下级区划返回；'
        || 'group_by=[enterprise_type] 时按旧接口返回最多 8 类企业类型（须字典映射）；'
        || 'group_by=[enterprise_scale] 时按从业人数返回五档企业规模；'
        || 'group_by=[industry_name] 时按旧接口返回最多 8 类行业名称（无字典映射）。',
    input_schema = '{
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
              "prefixItems": [
                {"enum": ["next_area", "enterprise_type", "enterprise_scale", "industry_name"]}
              ],
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
    updated_by = 'system-migration',
    updated_at = NOW(),
    etag = etag + 1
WHERE capability_id = 'governance.query_enterprise_metrics'
  AND version = '1.0.0';

INSERT INTO full_view_agent.schema_version (version)
SELECT 23
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 23
);

COMMIT;
