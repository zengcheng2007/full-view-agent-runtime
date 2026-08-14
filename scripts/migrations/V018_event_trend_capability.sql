-- V018: Extend the built-in event aggregate capability with the controlled
-- monthly event-total trend shape. Execution remains in the governed adapter;
-- this registry contract makes the accepted input visible after PG rebuilds.

BEGIN;

UPDATE full_view_agent.capability_tools
SET
    name = '查询治理事件指标',
    description = '查询授权区域的事件办结率快照，或在明确时间范围内按自然月汇总事件总数；月度趋势不表示上报量或处置量。',
    resource_path = '/event/getEventCountByMonth',
    input_schema = '{
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "query": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "metrics": {
              "type": "array",
              "items": {"enum": ["finish_rate", "event_count"]},
              "minItems": 1,
              "maxItems": 1
            },
            "scope": {
              "type": "object",
              "additionalProperties": false,
              "properties": {
                "area_code": {"type": "string", "pattern": "^[0-9]+$"},
                "include_descendants": {"type": "boolean", "default": true}
              },
              "required": ["area_code"]
            },
            "group_by": {
              "type": "array",
              "items": {"const": "month"},
              "maxItems": 1
            },
            "time_range": {
              "type": "object",
              "additionalProperties": false,
              "properties": {
                "start": {"type": "string", "format": "date"},
                "end": {"type": "string", "format": "date"}
              },
              "required": ["start", "end"]
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000}
          },
          "required": ["metrics", "scope"]
        }
      },
      "required": ["query"]
    }'::jsonb,
    output_schema = '{
      "oneOf": [
        {"data_schema_ref": "schema://data/event-finish-rate-table/1.0.0"},
        {"data_schema_ref": "schema://data/event-trend-table/1.0.0"}
      ]
    }'::jsonb,
    data_schema_ref = 'schema://data/event-finish-rate-table/1.0.0',
    parameter_mapping = '{
      "transport": "controlled-built-in-adapter",
      "monthly_event_total": {
        "method": "POST",
        "path": "/event/getEventCountByMonth",
        "body": {
          "area_name": "areaName",
          "area_code": "areaCode",
          "start": "startDate",
          "end": "endDate"
        }
      }
    }'::jsonb,
    updated_at = now(),
    updated_by = 'system-migration',
    etag = etag + 1
WHERE capability_id = 'governance.query_event_metrics'
  AND version = '1.0.0';

INSERT INTO full_view_agent.schema_version (version)
SELECT 18
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 18
);

COMMIT;
