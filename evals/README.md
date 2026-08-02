# Agent Eval/Replay

这里存放全量信息视图 Agent 的版本化行为基线。它不是针对某个模型写死的 Prompt
测试，而是验证完整运行链路能否在给定模型动作下可靠执行、拒绝和收敛。

## 当前基线

| 用例 | 主要门禁 |
| --- | --- |
| `planning-population-success` | 选择正确 Tool、成功终态、结果与 Evidence |
| `planning-population-http-success` | 真实身份准入、区划解析、正式 HTTP 人口查询与 Evidence |
| `planning-area-empty-http` | 真实区划空候选，不继续人口查询且不把未查询误报为零 |
| `policy-area-denied` | 授权区划外查询被 Policy 拒绝且不产生 Evidence |
| `policy-real-area-denied` | 真实身份越权请求允许模型主动拒绝或由 Policy 确定性拒绝 |
| `population-tool-timeout` | 可控注入人口 Tool 超时，停止模型层重复调用并保留根因 |
| `model-timeout` | 模型异常标准化为 `model_timeout` 并可靠终止 |
| `result-traceability` | 多 Tool 生命周期闭合，每个成功 Tool 都有持久化结果与 Evidence |

所有用例使用代码 Grader，发布基线要求 `pass@1 = 100%`。安全和权限判断不使用
LLM-as-judge。Grader 支持场景级回答必含/禁含断言，以及多个完整终态变体；终态变体必须
同时匹配 Outcome、Reason 和 Tool 序列，不能把任意成功或任意拒绝混为通过。

## Trace 边界

Trace 保存模型可见的上下文、实际模型步骤、用户可见事件类型、终态、Tool 序列、
Evidence ID、逐项评分、环境类型、证据来源系统和运行时版本。`run-live-http` 还会保存按
`method/path/count` 聚合的下游请求摘要；该摘要只用于审计，不进入模型上下文，也不包含
Header、Query 或 Body。Context Builder 不向模型暴露 `credential_ref`，Trace 也不保存
原始 Token、模型 API Key、Policy 指纹或 Credential 内容。旧 Trace 缺少新增字段时会以
`unknown` 和空请求列表加载，仍可兼容回放。

`replay` 使用 Trace 中已经消费的模型步骤重新运行相同用例，因此不访问外部模型，
适合复现 Runtime、Policy、Tool 或事件协议变更导致的回归。

`run-live-http` 只从当前进程环境读取 `FULL_VIEW_EVAL_GEO_TOKEN`，不会从 `.env`
读取或持久化该值；`FULL_VIEW_P0_ALLOWED_USER_IDS` 为空时拒绝启动。这样可以让模型配置
保持可复现，同时让旧系统登录凭据保持短期、进程级和显式授权。它与生产 API 共用同一组
生产 HTTP Tool Registry，不会暴露仅供静态评测使用的 `object_profile`。运行时版本优先
使用当前进程显式配置的 `FULL_VIEW_RUNTIME_VERSION`；未配置时使用 Git HEAD 的 12 位短
SHA，工作树有改动时追加 `-dirty`，Git 信息不可验证时才使用 `unknown`。Trace 只保存
该安全版本标识，不保存仓库路径或 Git 状态内容。

用例可通过 `fault.type=upstream_timeout` 对指定 Tool 做评测层故障注入。注入发生在下游
调用之前，仅用于验证 Agent 收敛和错误归因，不改变生产 HTTP Adapter 的实现。
