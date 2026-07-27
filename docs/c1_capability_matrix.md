# C1 能力矩阵：原系统业务能力与 Agent 接线核对 V1

> 日期：2026-07-27
> 范围：Gate C1 交付物（《19_客户演示能力接入与通用语义层并行计划V1》§4.2.1）
> 方法：`qxst-sj` 只读核对原系统真实接口与展示行为；`agent-runtime` 核对
> Adapter、生产 Registry、授权与模型可见性。URL/表名/字段名仅在本文件与
> Adapter 内部出现，不进入模型提示词、Tool 描述或输入 Schema。

## 1. 全局约定（来自 qxst-sj 只读核对）

- 业务后端前缀 `geo-qxst/`，登录/身份前缀 `geo-user/user/`
  （`qxst-sj/src/api/fetch.js:15-26`）；所有请求头携带 `geoToken`
  （`fetch.js:57-60`），用户数据范围来自 `geo-user/user/getUser` 的
  `areaCode`（`src/store/actions.js:16`）。
- `areaName` 参数承载“当前区划层级列名”：4→city_code、6→county_code、
  9→town_code、12→community_code、15→grid_code、17→courtyard_code、
  21→unifiedaddressid（`src/utils/modules/commonUtil.js:429-454`）。
  agent-runtime 的 `_area_code_column` 复刻同一映射
  （`src/full_view_agent/infrastructure/governance_adapter.py`）。
- 通用下级区划聚合端点 `POST geo-qxst/getNextSiteData` 按
  `tableName` 区分主题，返回每个下级区划一行
  `{areaCode, areaName, total, lon, lat, level}`
  （地图消费端 `src/pages/map/index.vue:1749-1774`）。

## 2. 能力矩阵

| 业务主题 | 原系统真实接口（geo-qxst） | 关键参数 | 响应字段 | 区域粒度 | 权限来源 | 是否服务端聚合 | Agent 接线状态（C1 后） | 缺失口径 |
|---|---|---|---|---|---|---|---|---|
| 区划（名称解析） | `POST area/getAreaInfoByAreaName`（原系统 AI 助手 `components/aiAssistant/index.vue:218`） | areaName（名称） | `{areacode, areaname}` | 单区划 | geoToken | 否（查找） | 生产已接线 `governance.resolve_area`；范围外候选过滤 | 无区划树接口接入（原系统 `area/getAreaMap` 未接） |
| 区划（下级树/逐区计数） | `POST area/getAreaMap`、`area/getNextCount`（`urlByType.js:3-37`） | areaCode、type | 树/逐子区计数 | 多级 | geoToken | 是 | 未接线 | 一期不作为 Tool；区划解析足够支撑演示问法 |
| 人口（独居老人） | `POST getNextSiteData`，`tableName=dm_empty_nest_old`（原系统专题分析 `subjectAnalysis/index.vue:241-243` 同族用法） | areaCode、areaName（层级列）、tableName | `{areaCode, areaName, total}` | 区县/街道/社区→直接下一级 | geoToken + 区域范围 | 是（固定表固定端点） | 生产已接线 `governance.query_population_metrics`；仅 `person_category=solitary_elderly`，group_by 仅直接下一级 | 年龄/性别筛选与分组：契约声明但真实端点不支持（HTTP Adapter 拒绝），列为 S0 遗留项 |
| 出租房（按租赁类型汇总） | `POST house/getRoomLeaseType`（看板 `mainBlockData/house/compoments/rentalHouse.vue:57-79`） | areaCode、areaName（层级列） | `[{house_type, total}]` | 区域自身 | geoToken + 区域范围 | 是 | 生产已接线 `governance.query_housing_metrics`（无 group_by 分支） | 无租赁类型筛选/排序参数（原系统亦无） |
| 出租房（按下级区划汇总） | `POST getNextSiteData`，`tableName=base_room_lease`（地图出租房图层 `map/modules/getFeature.js:304-305`、`urlByType.js:257-263`，消费端 `map/index.vue:1749-1774,1866`） | areaCode、areaName（层级列，市级为 `city_code`，`commonUtil.js:429-454` case 4）、tableName | `[{areaCode, areaName, total, lon, lat, level}]` | 市→区县、区县→街道、街道→社区、社区→网格 | geoToken + 区域范围 | 是（原系统地图层固定调用，非客户端推算） | C1 新接线：受控 `group_by=['next_area']`，类型化 `HousingAreaGroupTable` + Evidence | 网格级及以下无下级聚合，运行时以 `semantic_validation_error` 拒绝 |
| 出租房（明细列表） | `POST getSiteDataList`，`tableName=base_room_lease`（`detailWrap.vue:1505-1510`） | page、size | `{data|list, total}`，含房东姓名/电话 | 明细 | geoToken | 否（明细） | 不接线（一期不做个人明细，Doc 19 §2） | 敏感字段，禁止接入 |
| 网格事件（办结率） | `GET api/getEventPropertiesAndConflictsByTotal`（看板 `mainBlockData/event/component/gridevents/eventCompletion.vue:62-81`） | areaCodeValue、areaCodeName（层级列） | `{gridFinishRate, communityFinishRate, streetFinishRate, ...}` | 区域自身三层级 | geoToken + 区域范围 | 是 | E1 已接线 `governance.query_event_metrics`；生产 Registry、准入权限、模型可见性和 Evidence 已打通 | 仅为原系统当前快照；不支持时间范围、事件总量、办结数、下级区划明细和阈值筛选 |
| 网格事件（按类型/按区划） | `getSiteDataByType`（ES 聚合）、`area/getNextEventCount` | tableName=event_event_info 等 | `{key, doc_count}` 等 | 类型/子区 | geoToken | 是 | 未接线 | 时间范围与口径未核定 |
| 楼栋画像 | `POST house/getHouseDetails`（`searchDetails/api/indexApi.js:7-78`） | houseId（统一地址编码） | ES `_source`：名称、经纬度等 | 单对象 | geoToken + 对象区域校验 | 否（对象查询） | Adapter 已实现（building only，回包区域复核）；生产子集未包含 → 保持不可见 | 未在真实 HTTP 完成验证；字段分级与脱敏策略待 P1 核定 |
| 地图联动 | 前端 Frontend Command + 回执（08 号文档边界） | — | — | — | — | — | 人口分色图命令已接；出租房分色图命令未接（P1） | 出租房 choropleth 待前端联调验证 |

## 3. 生产接线五层核对（C1 后）

| 层 | 出租房（类型） | 出租房（next_area） | 人口 | 区划 | 事件 | 楼栋画像 |
|---|---|---|---|---|---|---|
| Adapter 存在（生产 HTTP） | ✓ | ✓（同族端点） | ✓ | ✓ | ✓ | ✓（未验证） |
| 生产 Registry 可见（HTTP 子集） | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ 保持排除 |
| 准入授权（entitlement+dataset） | ✓ | ✓ | ✓ | ✓ | ✓ | ✓（但 Registry 排除） |
| 模型可选择（授权过滤） | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| 运行验证（证据） | live-HTTP 已有运行记录 + C1 接线测试 | 真实登录环境已验证 | live-HTTP 已有运行记录 | live-HTTP 已有运行记录 | E1 真实登录环境已验证：区划解析 + 事件 Tool + 3 行 Result + `geo-qxst/event` Evidence | 无 |

## 4. 提示词/目录真实性

- 系统提示词版本 `full-view-governance-readonly-v9`：能力清单由注册表工具集
  与用户授权交集动态生成（`prompt_catalog.py` + `context_builder.py`）；
  生产 HTTP 模式仅展示已接线的区划、人口、出租房和事件能力，仍不展示画像。
- 模型可见的 Tool 描述与输入 Schema 不含端点 URL、物理表名、列名
  （`tests/test_housing_next_area.py::test_housing_model_surface_hides_physical_implementation`、
  `tests/test_production_wiring.py::test_trace_round_trip_does_not_expose_physical_names_to_model` 固化）。

## 5. 结论

- 出租房两类汇总均有原系统真实固定接口支撑：类型汇总走
  `house/getRoomLeaseType`，下级区划汇总走 `getNextSiteData`（表参数为适配器
  内部编译常量，不暴露给模型）。
- 市级“全市按区县汇总”与其余下级聚合同走 `getNextSiteData` 通用端点
  （`areaName=city_code`，`commonUtil.paramsLoader` case 4），C1 已接线并以
  契约测试固化（请求参数与区县响应）。
- 事件办结率当前快照已完成 E1 生产接线和真实登录验证；缺少任一必需办结率
  字段时按上游契约错误失败，不再把缺数据伪装为 `0%`。
- 楼栋画像仍维持“已实现但未接线、不可见”，等待真实 HTTP 验证后按
  Doc 19 §4.2.4 接通。
