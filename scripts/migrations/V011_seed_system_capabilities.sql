-- V011: Seed System Capabilities
-- R3-B1: Establish the four governance seed capabilities as versioned
-- system capabilities in the capability center (single source of truth).
-- These are idempotent: ON CONFLICT DO NOTHING ensures we don't overwrite
-- admin modifications.

-- =============================================================================
-- Seed connector for governance adapter (internal geo-qxst gateway)
-- =============================================================================
INSERT INTO full_view_agent.connectors (
    connector_id, name, base_url, description,
    allowed_path_prefixes, is_active, timeout_ms
) VALUES (
    'governance-geo-qxst',
    'governance-gateway',
    'http://127.0.0.1:9666',
    'Local governance data gateway proxying geo-qxst backend',
    ARRAY['/geo-qxst'],
    true,
    8000
) ON CONFLICT (connector_id) DO NOTHING;

-- =============================================================================
-- Seed Capability 1: 标准区划解析 (governance.resolve_area)
-- =============================================================================
INSERT INTO full_view_agent.capability_tools (
    capability_id, name, domain, owner, version, status,
    risk_level, required_permissions, dataset_ids, description,
    connector_ref, http_method, resource_path,
    input_schema, result_kind, max_result_rows,
    cache_enabled, cache_ttl_seconds,
    created_by, updated_by
) VALUES (
    'governance.resolve_area',
    '解析区划',
    'governance',
    'full-information-domain-team',
    '1.0.0',
    'published',
    'low',
    ARRAY['governance.area.read'],
    ARRAY['administrative_area'],
    '将区划名称或当前区域表达解析为授权范围内的标准区划候选。',
    'governance-geo-qxst',
    'POST',
    '/geo-qxst/resolve-area',
    '{"type": "object", "properties": {"area_name": {"type": "string"}, "parent_code": {"type": "string"}}, "required": ["area_name"]}',
    'area_candidates',
    100,
    true, 300,
    'system-seed', 'system-seed'
) ON CONFLICT (capability_id, version) DO NOTHING;

-- =============================================================================
-- Seed Capability 2: 人口指标查询 (governance.query_population_metrics)
-- =============================================================================
INSERT INTO full_view_agent.capability_tools (
    capability_id, name, domain, owner, version, status,
    risk_level, required_permissions, dataset_ids, description,
    connector_ref, http_method, resource_path,
    input_schema, result_kind, max_result_rows,
    cache_enabled, cache_ttl_seconds,
    created_by, updated_by
) VALUES (
    'governance.query_population_metrics',
    '查询人口指标',
    'governance',
    'full-information-domain-team',
    '1.0.0',
    'published',
    'low',
    ARRAY['governance.population.aggregate.read'],
    ARRAY['population'],
    '查询授权区域的人口聚合指标（独居老人等），不返回个人明细。',
    'governance-geo-qxst',
    'POST',
    '/geo-qxst/population-metrics',
    '{"type": "object", "properties": {"query": {"type": "object"}}, "required": ["query"]}',
    'table',
    1000,
    true, 60,
    'system-seed', 'system-seed'
) ON CONFLICT (capability_id, version) DO NOTHING;

-- =============================================================================
-- Seed Capability 3: 出租房指标查询 (governance.query_housing_metrics)
-- =============================================================================
INSERT INTO full_view_agent.capability_tools (
    capability_id, name, domain, owner, version, status,
    risk_level, required_permissions, dataset_ids, description,
    connector_ref, http_method, resource_path,
    input_schema, result_kind, max_result_rows,
    cache_enabled, cache_ttl_seconds,
    created_by, updated_by
) VALUES (
    'governance.query_housing_metrics',
    '查询出租房指标',
    'governance',
    'full-information-domain-team',
    '1.0.0',
    'published',
    'low',
    ARRAY['governance.housing.aggregate.read'],
    ARRAY['housing'],
    '查询授权区域的出租房聚合统计，不返回个人明细。支持按租赁类型和下级区划分组。',
    'governance-geo-qxst',
    'POST',
    '/geo-qxst/housing-metrics',
    '{"type": "object", "properties": {"query": {"type": "object"}}, "required": ["query"]}',
    'table',
    1000,
    true, 60,
    'system-seed', 'system-seed'
) ON CONFLICT (capability_id, version) DO NOTHING;

-- =============================================================================
-- Seed Capability 4: 事件治理指标查询 (governance.query_event_metrics)
-- =============================================================================
INSERT INTO full_view_agent.capability_tools (
    capability_id, name, domain, owner, version, status,
    risk_level, required_permissions, dataset_ids, description,
    connector_ref, http_method, resource_path,
    input_schema, result_kind, max_result_rows,
    cache_enabled, cache_ttl_seconds,
    created_by, updated_by
) VALUES (
    'governance.query_event_metrics',
    '查询网格事件办结率',
    'governance',
    'full-information-domain-team',
    '1.0.0',
    'published',
    'low',
    ARRAY['governance.event.aggregate.read'],
    ARRAY['event'],
    '查询指定授权区域的网格事件汇总办结率。',
    'governance-geo-qxst',
    'POST',
    '/geo-qxst/event-metrics',
    '{"type": "object", "properties": {"query": {"type": "object"}}, "required": ["query"]}',
    'table',
    1000,
    true, 60,
    'system-seed', 'system-seed'
) ON CONFLICT (capability_id, version) DO NOTHING;

-- =============================================================================
-- Record migration version
-- =============================================================================
INSERT INTO full_view_agent.schema_version (version) VALUES (11)
ON CONFLICT DO NOTHING;
