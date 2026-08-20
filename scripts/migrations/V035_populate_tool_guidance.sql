-- V035: Populate guidance for existing published tools.
--
-- WSZC-14: 引导与定义合一。将当前硬编码在 prompt_catalog.py 中的能力引导
-- 回写到 capability_tools 表，使每个已发布工具自带 guidance。
-- 迁移后 Runtime 从 DB 读取引导，硬编码代码可安全删除。

BEGIN;

-- 1. knowledge.search
UPDATE full_view_agent.capability_tools
SET guidance = (
    'knowledge.search：检索当前用户在当前应用中获权且已发布的知识库；'
    '使用返回内容作答时必须标注文档、知识库版本及片段或页码/段落引用。'
),
display_order = 10,
updated_at = NOW(),
etag = etag + 1
WHERE capability_id = 'knowledge.search'
  AND guidance = '';

-- 2. governance.resolve_area
UPDATE full_view_agent.capability_tools
SET guidance = (
    'resolve_area：需要把区划名称转换为标准区划编码时使用。'
),
display_order = 20,
updated_at = NOW(),
etag = etag + 1
WHERE capability_id = 'governance.resolve_area'
  AND guidance = '';

-- 3. governance.query_population_metrics
UPDATE full_view_agent.capability_tools
SET guidance = (
    'query_population_metrics：查询一般人口或独居老人聚合指标，不支持年龄或性别统计。'
    '一般人口不传 filters；独居老人必须传'
    '[{field:''person_category'',operator:''eq'',value:''solitary_elderly''}]。'
    '人口 Tool 的 group_by 规则：区县按街道汇总传 group_by=[''street'']，'
    '街道按社区汇总传 group_by=[''community'']，社区按网格汇总传 group_by=[''grid'']。'
    '全市人口排名使用受控分组：区县传 group_by=[''district'']，全市街道传 '
    'group_by=[''descendant_street'']，全市社区传 '
    'group_by=[''descendant_community'']；必须按 person_count 排序并设置 TopN limit。'
),
display_order = 30,
updated_at = NOW(),
etag = etag + 1
WHERE capability_id = 'governance.query_population_metrics'
  AND guidance = '';

-- 4. governance.query_housing_metrics
UPDATE full_view_agent.capability_tools
SET guidance = (
    'query_housing_metrics：按上游当前返回的出租类型动态汇总区域自身数据，'
    '类型集合由业务数据决定，不预设固定完整枚举；'
    '传 metrics=[''building_count'',''room_count''] 且不分组时，'
    '返回区域楼幢总数与户室总数；'
    '不传 group_by 时按租赁类型汇总；'
    '传 group_by=[''room_use''] 时按户室用途分类汇总并返回中文用途名称；'
    '传 group_by=[''next_area''] 时返回直接下级区划'
    '（全市按区县、区县按街道、街道按社区、社区按网格）的出租房数量分布；'
    '除 room_use、next_area 外不支持其他分组，也不支持筛选、排序。'
),
display_order = 40,
updated_at = NOW(),
etag = etag + 1
WHERE capability_id = 'governance.query_housing_metrics'
  AND guidance = '';

-- 5. governance.query_event_metrics
UPDATE full_view_agent.capability_tools
SET guidance = (
    'query_event_metrics：metrics=[''finish_rate''] 且不分组时，查询'
    '指定区域自身的网格、社区、街道三个层级办结率快照；'
    'metrics=[''event_count'']、group_by=[''month''] 时，必须传'
    ' yyyy-MM-dd 的 time_range，起始不早于 2021-01-01，且最多'
    ' 24 个自然月，返回事件总数月度趋势；不得表述为上报或处置趋势；'
    '所有形态均不支持按阈值筛选。'
    'metrics=[''event_count'']、group_by=[''event_category''] 且不传时间范围时，'
    '返回现有主题块口径的网格事件一级分类统计，不得称为全量事件；'
),
display_order = 50,
updated_at = NOW(),
etag = etag + 1
WHERE capability_id = 'governance.query_event_metrics'
  AND guidance = '';

-- 6. governance.get_object_profile
UPDATE full_view_agent.capability_tools
SET guidance = (
    'get_object_profile：查询声明区域内楼栋的基础画像和位置；'
    '当前真实适配器只支持 building，调用时必须提供 scope。'
),
display_order = 60,
updated_at = NOW(),
etag = etag + 1
WHERE capability_id = 'governance.get_object_profile'
  AND guidance = '';

-- 7. governance.get_governance_overview
UPDATE full_view_agent.capability_tools
SET guidance = (
    'get_governance_overview：查询授权区域的人、房、企、事、物治理关联数、'
    '要素总数和治理覆盖率。使用全量信息视图当前四平台治理口径，不返回对象明细。'
),
display_order = 70,
updated_at = NOW(),
etag = etag + 1
WHERE capability_id = 'governance.get_governance_overview'
  AND guidance = '';

-- 8. governance.query_governance_power_metrics
UPDATE full_view_agent.capability_tools
SET guidance = (
    'query_governance_power_metrics：查询授权区域的户数、网格长、网格指导员、'
    '专职和兼职网格员、其他网格力量及微网格汇总，不返回姓名、电话或个人明细。'
),
display_order = 80,
updated_at = NOW(),
etag = etag + 1
WHERE capability_id = 'governance.query_governance_power_metrics'
  AND guidance = '';

-- 9. governance.query_enterprise_metrics
UPDATE full_view_agent.capability_tools
SET guidance = (
    'query_enterprise_metrics：查询授权区域内企业聚合数量，不返回企业明细。'
    'group_by=[next_area] 时按直接下级区划返回，'
    '结果可用于表格、柱状图和区划分色图；'
    'group_by=[enterprise_type] 时按旧接口返回最多 8 类企业类型，'
    '企业类型须经 enterprise_type 字典映射为中文，'
    '结果仅用于表格和柱状图；'
    'group_by=[enterprise_scale] 时按从业人数返回五档企业规模，'
    '旧接口的 10-50人、50-100人实际边界分别为'
    '11-50人、51-100人，且从业人数为空的企业不计入；'
    'group_by=[industry_name] 时按旧接口返回最多 8 类行业名称，'
    '行业名称直接来自上游响应，无须字典映射，'
    '结果仅用于表格和柱状图。'
),
display_order = 90,
updated_at = NOW(),
etag = etag + 1
WHERE capability_id = 'governance.query_enterprise_metrics'
  AND guidance = '';

COMMIT;
