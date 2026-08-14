-- V019: Publish the verified event-category shape on the existing built-in
-- event aggregate capability. The governed adapter remains authoritative.

BEGIN;

UPDATE full_view_agent.capability_tools
SET
    description = '查询授权区域的事件办结率快照、事件总数月度趋势，或现有主题块统计口径的网格事件一级分类；分类统计不代表全量事件。',
    input_schema = jsonb_set(
        input_schema,
        '{properties,query,properties,group_by,items}',
        '{"enum": ["month", "event_category"]}'::jsonb,
        true
    ),
    output_schema = '{
      "oneOf": [
        {"data_schema_ref": "schema://data/event-finish-rate-table/1.0.0"},
        {"data_schema_ref": "schema://data/event-trend-table/1.0.0"},
        {"data_schema_ref": "schema://data/event-category-table/1.0.0"}
      ]
    }'::jsonb,
    parameter_mapping = parameter_mapping || '{
      "event_category": {
        "method": "GET",
        "path": "/api/getEventProperties",
        "eventType": "eventtype_code1",
        "dictionary_method": "POST",
        "dictionary_path": "/dict/getDictValue"
      }
    }'::jsonb,
    updated_at = now(),
    updated_by = 'system-migration',
    etag = etag + 1
WHERE capability_id = 'governance.query_event_metrics'
  AND version = '1.0.0';

INSERT INTO full_view_agent.schema_version (version)
SELECT 19
WHERE NOT EXISTS (
    SELECT 1 FROM full_view_agent.schema_version WHERE version = 19
);

COMMIT;
