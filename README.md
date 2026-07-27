# 全量信息视图 Agent Runtime

这是全量信息视图智能体的 P0 Python 运行时，当前用于把
[P0 Agent 契约设计 V1.1](../03_P0_Agent契约设计V1.1.md)
落实成可运行、可测试、可导出契约的代码骨架。

## 当前已实现

- `geoToken` Header 接入；原始 Token 不写入 Session、Run、事件或 Tool 结果；
- Session、Run 状态机，以及“一个 Session 同时只有一个活动 Run”；
- Session 工作台接口：按更新时间分页列出、详情恢复、重命名和归档；列表和更新均
  强制用户归属隔离，活动 Run 通过 `active_run_id` 在刷新后恢复；
- Run 创建时原子保存用户 Message；支持按 Session 使用签名游标查询消息；
- Run 完成时保存带 Result/Evidence 引用的 assistant Message，页面刷新后可恢复对话；
- 创建 Session、Run、Steer 的幂等重放和冲突检测；
- Run 查询、SSE 事件流、`Last-Event-ID` 断点续传；
- Run 取消和运行中追加指令（Steer）；
- 异步 Mock Tool 执行与强类型人口指标表格结果；
- Tool Result 保存、`result.available` 事件和按资源归属查询；
- `resolve_area`、`query_population_metrics`、`query_housing_metrics`、
  `query_event_metrics`、`get_object_profile` 专用输入输出契约；生产 HTTP
  当前开放区划、人口、出租房和事件办结率快照，楼栋画像仍保持不可见；
- Tool Registry、内部 Manifest、模型 Descriptor、最小 Policy 与 Capability Service；
- 区划范围、数据集、Tool 权限、敏感字段集和 Policy 指纹强制校验；
- 加密 Credential Broker、吊销/过期处理和 `waiting_input/reauth` 恢复；
- PostgreSQL 权威存储，覆盖 Session、Run、Result、AuthContext、Event、
  幂等记录、输入请求、Steer 和 Credential；
- Redis 跨实例 SSE 唤醒、`Last-Event-ID` 续传及事件历史过期 410；Redis
  故障时自动回退 PostgreSQL 轮询；
- Result Payload 签名游标分页、Payload 独立 TTL、过期元数据保留和 Evidence 查询；
- 定向 `panel.show_table` FrontendCommand、PostgreSQL 命令/回执持久化和幂等
  CommandReceipt；错误客户端不能代替目标标签页提交回执；
- 统一错误信封、四类等待输入契约与标准 `input.required` 事件；当前执行链路已接通
  `reauth` 恢复；
- Base Harness 多轮循环，强制模型轮次、Tool 次数、连续失败、无进展、执行时间和
  重复调用预算；预算耗尽或循环命中会可靠收敛为 `failed` 终态；
- Provider-neutral 的 Model Provider、权限裁剪 Context Builder 和动态 Planner；
  当前支持 OpenAI Chat Completions 兼容接口，并叠加每 Run Token 预算；
- 版本化 Eval/Replay：使用 YAML 固定用例驱动真实运行组件，代码 Grader 检查
  终态、Tool 序列、生命周期、Evidence 与事件链，并可用脱敏 Trace 离线回放；
- 有界上下文组装：超过 20 条消息自动截断并生成结构化摘要，避免上下文无限增长；
- 确定性完成校验（`DeterministicCompletionValidator`）：要求非空摘要 + 至少一个
  Tool 结果或直接回答标记，防止模型幻觉式"成功"；
- 可显式启用的 `HttpGovernanceAdapter`：通过 Run Credential 调用现有
  `geo-qxst` 区划解析和独居老人下级区划统计接口，旧接口字段采用服务端白名单映射；
- 服务启动时扫描并恢复 `queued/running` Run，等待输入/审批的 Run 保持等待；
- OpenAPI 和 JSON Schema 单一注册表导出，并校验提交生成物无漂移；
- 单元测试、API/数据库/Redis 集成测试、Ruff 和 Pyright 静态检查。

`development/test` 模式下，未配置 `FULL_VIEW_DATABASE_URL` 时可使用内存
Adapter 和确定性 Planner 进行单元测试及快速开发。`production` 模式下必须配置
PostgreSQL、Credential/Cursor 密钥、HTTP 业务 Adapter 与 OpenAI-compatible
Model Provider，缺少任一项都会拒绝启动。身份
默认通过现有 `geo-gateway` `/getUserByToken` 解析；测试可显式注入
散列身份 Adapter。HTTP 业务 Adapter 已接入 `geo-qxst` 的区划解析和独居老人
统计接口，未实现的对象画像 Tool 不会在 HTTP 模式下注册。

## 目录

```text
agent-runtime/
├─ src/full_view_agent/
│  ├─ api/                 # FastAPI 接口和 Header 身份边界
│  ├─ application/         # Session/Run 用例与 Mock 执行器
│  ├─ domain/              # Pydantic 领域契约
│  └─ infrastructure/      # PostgreSQL/内存 Store、事件、凭据和现有系统 Adapter
├─ scripts/                # 契约导出脚本
├─ tests/                  # 单元与 API 集成测试
└─ pyproject.toml
```

生成的机器契约位于相邻的 `../contracts/`：

```text
contracts/
├─ openapi/agent-api-v1.yaml
└─ schemas/
   ├─ agent/
   ├─ tools/
   └─ data/tool-specific/
```

## 本地运行

需要 Python 3.12～3.15 和 `uv`。在本目录执行：

```powershell
uv sync
$env:FULL_VIEW_RUNTIME_PROFILE = "production"
$env:FULL_VIEW_DATABASE_URL = "postgresql://agent_user:password@127.0.0.1:15432/agent_db"
$env:FULL_VIEW_POSTGRES_SCHEMA = "full_view_agent"
$env:FULL_VIEW_LANGGRAPH_POSTGRES_SCHEMA = "full_view_agent_langgraph"
$env:FULL_VIEW_CREDENTIAL_KEY = [Convert]::ToBase64String(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
)
$env:FULL_VIEW_CURSOR_KEY = [Convert]::ToBase64String(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
)
$env:FULL_VIEW_REDIS_URL = "redis://127.0.0.1:16379/0"
$env:FULL_VIEW_P0_ALLOWED_USER_IDS = "replace-with-authorized-user-id"
$env:FULL_VIEW_GOVERNANCE_ADAPTER = "http"
$env:FULL_VIEW_GOVERNANCE_BASE_URL = "http://127.0.0.1:9666/geo-qxst"
$env:FULL_VIEW_ORCHESTRATOR = "langgraph"  # R3 默认；native 仅用于限时回滚
$env:FULL_VIEW_MODEL_PROVIDER = "openai_compatible"
$env:FULL_VIEW_MODEL_BASE_URL = "https://replace-with-model-endpoint/v1"
$env:FULL_VIEW_MODEL_NAME = "replace-with-model-name"
$env:FULL_VIEW_MODEL_API_KEY = "replace-with-model-key"
$env:FULL_VIEW_MODEL_TIMEOUT_SECONDS = "60"
$env:FULL_VIEW_MODEL_TOKEN_BUDGET = "32000"
uv run python -m full_view_agent.server
```

该入口在 Windows 上显式使用 `SelectorEventLoop`，避免异步 PostgreSQL 驱动与默认
Proactor 事件循环不兼容。需要热更新时可在非 Windows 环境直接使用 `uvicorn --reload`。

启动后可访问：

- OpenAPI UI：`http://127.0.0.1:8000/docs`
- OpenAPI JSON：`http://127.0.0.1:8000/openapi.json`
- 存活探针：`http://127.0.0.1:8000/health/live`
- 就绪探针：`http://127.0.0.1:8000/health/ready`（检查 PostgreSQL 和 Redis 连通性）

本地旧业务库没有独居老人联调数据时，可向一次性开发库导入幂等夹具：

```powershell
Get-Content -Raw scripts/seed_legacy_e2e.sql |
  docker exec -i tydzgl-postgres psql -U tydzgl -d geo_qxst
```

该脚本仅补充 `agent-e2e-*` 测试记录；如果旧库不存在
`dm_empty_nest_old`，会创建一个基于 `base_ppl_older` 的兼容视图。

工作台依赖的 Session 接口：

- `GET /agent-api/v1/sessions`：签名游标分页，可按 `active/archived` 筛选；
- `GET /agent-api/v1/sessions/{session_id}`：恢复会话和活动 Run；
- `PATCH /agent-api/v1/sessions/{session_id}`：重命名或归档，要求
  `Idempotency-Key`；活动 Run 未结束时禁止归档；
- `GET /agent-api/v1/sessions/{session_id}/messages`：恢复持久化消息。

创建 Session 的 PowerShell 示例：

```powershell
$headers = @{
  "geoToken" = "replace-with-test-token"
  "Idempotency-Key" = [guid]::NewGuid().ToString()
}
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/agent-api/v1/sessions" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body '{"title":"独居老人分析"}'
```

## 验证与契约导出

```powershell
uv run pytest
uv run ruff check src tests scripts
uv run pyright
uv run python scripts/export_contracts.py
```

定期清理过期数据（建议 cron 每日执行）：

```powershell
$env:FULL_VIEW_DATABASE_URL = "postgresql://agent_user:password@127.0.0.1:15432/agent_db"
$env:FULL_VIEW_RETENTION_DAYS = "30"
uv run python scripts/cleanup_expired.py
```

运行八个内置 Agent 基线评测并输出报告与可回放 Trace：

```powershell
uv run python scripts/run_evals.py run `
  --cases evals/cases `
  --output evals/runs/latest
```

使用 `.env` 中配置的真实 OpenAI Compatible 模型运行单个非阻断评测：

```powershell
uv run python scripts/run_evals.py run-live `
  --case evals/cases/planning-population-success.yaml `
  --output evals/runs/live-current/planning-population-success.json `
  --env-file .env
```

使用真实模型、现有登录身份和正式 HTTP Adapter 运行 S1 实景评测时，必须在当前进程
临时注入已签发的 `geoToken`，并显式配置 P0 用户白名单。不要把 Token 写入 `.env`、
命令参数或评测文件：

```powershell
$env:FULL_VIEW_EVAL_GEO_TOKEN = "replace-with-temporary-geo-token"
$env:FULL_VIEW_P0_ALLOWED_USER_IDS = "replace-with-authorized-user-id"
uv run python scripts/run_evals.py run-live-http `
  --case evals/cases/planning-population-http-success.yaml `
  --output evals/runs/live-current/planning-population-http-success.json `
  --env-file .env
Remove-Item Env:FULL_VIEW_EVAL_GEO_TOKEN
```

出租房纵向切片提供两个同类用例（按租赁类型汇总、按下级区划汇总）：
`evals/cases/planning-housing-http-success.yaml` 与
`evals/cases/planning-housing-next-area-http-success.yaml`，
运行方式与上面一致，只需替换 `--case` 与 `--output`。

事件办结率快照的生产路径用例为
`evals/cases/planning-event-http-success.yaml`，开放式真实模型用例为
`evals/cases-live/open-event-finish-rate-query.yaml`。该能力仅复用原系统当前
三层办结率，不支持时间范围、事件总量、办结数、下级区划明细或阈值筛选。

真实评测 Trace 会记录模型提供方、模型名、Prompt 版本、模型动作、Token 用量和评分，
但不会记录模型 API Key 或下游凭据。当前 System Prompt 版本为
`full-view-governance-readonly-v9`。模型返回的对象字段若被二次编码为 JSON 字符串，
运行时只对 Schema 明确定义为对象的字段执行一次兼容解码，随后仍须通过强类型和权限校验。
成功、部分成功或拒绝的 Tool 不会在同一 Run 中再次暴露给模型，避免模型重复执行已经终止的
动作；`upstream_timeout`、`upstream_unavailable` 和 `upstream_contract_error` 也会在
Adapter 内部重试耗尽后停止向模型暴露，参数或语义错误仍允许修正后重试。其他尚未执行的
Tool 仍可继续用于多步任务。

离线回放其中一个用例，不请求模型服务：

```powershell
uv run python scripts/run_evals.py replay `
  --case evals/cases/planning-population-success.yaml `
  --trace evals/runs/latest/traces/planning-population-success.json `
  --output evals/runs/latest/replay-planning-population-success.json
```

评测用例和门禁规则见 [evals/README.md](evals/README.md)。

真实 PostgreSQL 集成测试使用独立临时 schema，不修改业务表：

```powershell
$env:FULL_VIEW_TEST_DATABASE_URL = $env:FULL_VIEW_DATABASE_URL
$env:FULL_VIEW_TEST_REDIS_URL = $env:FULL_VIEW_REDIS_URL
uv run pytest tests/test_postgres_persistence.py
```

## 容器化部署

```bash
docker build -t full-view-agent-runtime .
docker run -d --name agent-runtime \
  -p 8000:8000 \
  -e FULL_VIEW_RUNTIME_PROFILE=production \
  -e FULL_VIEW_DATABASE_URL=postgresql://agent_user:password@host:15432/agent_db \
  -e FULL_VIEW_POSTGRES_SCHEMA=full_view_agent \
  -e FULL_VIEW_LANGGRAPH_POSTGRES_SCHEMA=full_view_agent_langgraph \
  -e FULL_VIEW_CREDENTIAL_KEY=<base64-32-bytes> \
  -e FULL_VIEW_CURSOR_KEY=<base64-32-bytes> \
  -e FULL_VIEW_REDIS_URL=redis://redis:16379/0 \
  -e FULL_VIEW_P0_ALLOWED_USER_IDS=<user-id> \
  -e FULL_VIEW_GOVERNANCE_ADAPTER=http \
  -e FULL_VIEW_GOVERNANCE_BASE_URL=http://host:9666/geo-qxst \
  -e FULL_VIEW_MODEL_PROVIDER=openai_compatible \
  -e FULL_VIEW_MODEL_BASE_URL=https://model-endpoint/v1 \
  -e FULL_VIEW_MODEL_NAME=model-name \
  -e FULL_VIEW_MODEL_API_KEY=model-key \
  full-view-agent-runtime
```

数据库初始化使用独立迁移脚本：

```bash
psql -U agent_user -d agent_db -f scripts/migrations/V001_initial_schema.sql
psql -U agent_user -d agent_db -f scripts/migrations/V002_checkpoint_mapping.sql
```

`FULL_VIEW_ORCHESTRATOR=langgraph` 首次运行时会在独立
`FULL_VIEW_LANGGRAPH_POSTGRES_SCHEMA` 中调用官方 Checkpointer 的 `setup()`；
产品侧只通过 V002 映射表记录 Run、线程和最新 checkpoint 的关系，不读取
LangGraph 私有表。生产数据库账号需要具备该框架 Schema 的建表权限。

## 后续实现方向

1. 实现对象画像 Tool 的正式业务 Adapter，并继续扩充可治理的 Tool Registry；
2. 扩充 S1 的多轮追问、字段脱敏和部分失败实景回归，并为 S2—S5 建立正式 HTTP Adapter；
3. 将已完成的 `map.render_choropleth` 命令适配到后续封板场景，并补齐分页、等待输入和
   正式菜单/网关配置。

当前 `qxst-sj` 已支持 `panel.show_table` 和 `map.render_choropleth`。地图命令只引用
持久化 Result，不接收模型生成的 GeoJSON 或任意样式；浏览器在区划边界和图层完成后提交
幂等 CommandReceipt。

人大金仓迁移在实现稳定后单独验证；当前表使用 TEXT、TIMESTAMPTZ、BYTEA 等基础类型，
不依赖 pgvector、JSONB 或其他 PostgreSQL 插件。Qdrant 和 MinIO 不在本批次范围内。
