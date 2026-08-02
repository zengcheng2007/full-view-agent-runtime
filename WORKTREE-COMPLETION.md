# P1-G 结构化可信回答第一阶段交付报告

## 交付状态

- 状态：第一阶段最小闭环完成，等待 P1-R reviewer 审核。
- 模式：编码智能体 V3.2 `module-dev`，独立 worktree。
- 分支：`codex/p1-structured-finish`。
- 边界：未合并、未推送；未修改前端、数据库、权限、业务 Tool、部署配置或
  Eval 运行产物。

## 实现结果

1. 新增 provider-neutral `StructuredFinish` / `AnswerClaim`：
   - 每条 Claim 明确绑定 `result_id`、`result_fingerprint`、集合、行定位、
     字段、运算和值；
   - 第一阶段支持 `rows/root` 与 `value/sum/count/min/max/is_min/is_max/all_equal`；
   - OpenAI 兼容协议通过保留函数 `full_view__finish_answer` 提交。
2. `ModelPlanner` 每轮追加 Finish Tool，并在 Planner 内截获：
   - Finish 不进入业务 Tool、权限或执行链路；
   - 真实生产 Planner 默认不接受普通文本作为成功数据回答；
   - 旧脚本 Eval 只能通过 `allow_legacy_finish=True` 显式兼容。
3. Harness 继续作为 Native/LangGraph 唯一完成校验边界：
   - 确定性核验 Result、指纹、唯一行、字段、Decimal 值、聚合和极值；
   - 多 Result 的每条 Claim 独立绑定，A/B 同标签值不能互换；
   - `truncated` Result 禁止所有依赖全量数据的聚合、极值和全等判断；
   - 通过后由服务端生成事实文本，不展示模型提供的事实正文；
   - `reference_only` 输出固定数据面板文案。
4. Fail-closed：
   - 有 success/partial Result 时，生产普通文本进入
     `structured_claims_required` 修订；
   - Finish Tool schema 只暴露已实现的 `claims/reference_only`；
   - 仅允许一次修订，第二次仍缺失或非法时返回固定安全结束语，不触发
     `loop_detected`。
5. Eval：
   - `EvalFinishStep` 保存完整 `structured_finish`；
   - Recording/Scripted provider 可录制和回放 Claim；
   - 回放时按稳定 `result_fingerprint` 将录制 Claim 重新绑定到本次 Result ID；
   - Tool 观察增加 `result_fingerprint`，未增加凭据或内部地址。
6. Prompt 升级为 `full-view-governance-readonly-v13`，明确结构化完成约束。

## Reviewer 阻断修复

- `all_equal` 只接受可证实为真的声明；两行不等时，即使模型提交
  `value=false` 也拒绝，避免渲染成“全部相同”。
- 历史 Result 尚未水合进 Harness 时，只允许结构化 `reference_only`，并输出
  服务端固定文案；普通文本和 claims 均修订一次后安全停止。
- 无当前或历史 Result 时，`reference_only/claims` 不能借普通文本白名单通过。
- reserved finish 参数缺字段或非法枚举时转为可修订完成动作；多 Tool、未知业务
  Tool 等协议错误仍硬失败。Native/LangGraph 均覆盖二次错误安全停止且无 loop。
- v13 提示和历史上下文同步降级：只提供 Result 引用元数据，不提供历史行载荷，
  明确禁止复述、排序、筛选、计算或解释。
- HTTP 住房类型及 InMemory 住房类型/下级区划统一执行 `limit + truncated`；
  上游显式截断标志同样保留，截断结果的全局聚合 Claim 被拒绝。
- 生产 `ModelPlanner` 的无结果普通文本不再借关键词白名单完成；`legacy=False`
  一律进入一次修订，第二次仍违规时固定安全停止。
- Finish Tool 完整支持 capability/clarification/denial/failure：前两者仅允许无结果
  状态，后两者必须绑定匹配的 denied/failed ToolResult，全部使用服务端固定模板。
- Native/LangGraph 已覆盖“有授权 Tool、零 Tool 调用、伪造能力正文”的反例，
  并覆盖正常 capability/clarification 无模型数字泄漏。
- `FinishAction.legacy` 默认值改为 `False`；新建或自定义 Planner 省略参数时自动
  fail-closed。旧文本门禁测试、服务端固定文案重评和兼容 Planner 必须逐点显式
  `legacy=True`，并在代码中说明信任来源。

## TDD 证据

先观察到预期 RED，再完成 GREEN，新增覆盖包括：

- 错值、错误指纹、行零匹配/多匹配、字段不存在；
- 两个 Result 同标签时跨来源换值；
- 错误 `is_max`；
- 截断结果的 `sum/is_max`；
- `sum/count/min/max/all_equal` 确定性复算；
- `all_equal` 不能以单行伪装全等；
- `rows` 与 `root` 两种集合；
- 普通文本修订一次后安全终止；
- `reference_only` 忽略模型错误正文；
- Finish Tool schema 不再暴露未实现的 capability/clarification/denial/failure；
- malformed structured finish 在两个编排器中修订一次后安全停止；
- HTTP/InMemory 住房 limit 截断贯通到 Claim 聚合拒绝；
- OpenAI Finish Tool 往返；
- Native/LangGraph 同一结构化 Claim 产生相同事实文本；
- live Eval 录制后 Scripted replay 不丢 Claim。

## Fresh 验证

- 全量测试：`597 passed, 18 skipped`（`615 collected`）。
- Ruff：`All checks passed!`。
- Pyright：`0 errors, 0 warnings, 0 informations`。
- Compileall：通过。
- Eval：`46/46 pass@1=100%`。
- Native/LangGraph 差分：`46 cases, 0 differences`。

## 明确未完成（第二阶段）

- 历史 Result 载荷水合后重新核验追问 Claim；当前只保留既有历史
  `result_id/evidence_id` 信任边界。
- 单条 Claim 的跨 Result 计算、百分比、趋势和表达式树。
- 更自然的类型化 Answer Block/领域字段显示名；第一阶段采用服务端通用事实文本。
- 删除旧自由文本正则兼容门禁；当前仅供显式 legacy Eval/既有内部 Planner 使用。

## Reviewer 重点

1. 生产 `ModelPlanner` 是否存在绕过 Finish Tool 的普通文本成功路径。
2. Result ID、fingerprint、row locator 和 operation 是否全部 fail-closed。
3. Scripted replay 的 fingerprint 重绑定在多个同指纹 Result 时是否保持拒绝。
4. Native/LangGraph 是否只复用 Harness，没有复制 Claim 校验逻辑。

---

# P1 业务标签服务端渲染补丁

## 交付结果

- `PopulationMetricRow` / `HousingLeaseTypeRow` / `HousingAreaGroupRow` /
  `EventFinishRateRow` 以 Pydantic `Field` 元数据作为字段标签、单位和枚举
  展示名的唯一来源。
- Claims 仍只接受 logical field 与原始值；服务端从已验证
  `DataResult.data` 的实际行模型解析展示元数据，未知元数据回退到
  logical field/raw value。
- 模型无法传入或伪造 `label/unit`；单位与枚举映射不取自
  模型正文。
- 数值统一经 `Decimal` 安全格式化，整数不会回退为 `85.0`。
- 典型输出：`住宅出租的出租房数量为884套。`、`网格的办结率为85%。`。
- 展示元数据不进入 `model_dump`，Result 指纹仍只基于数据值。

## 生成契约

使用现有 `scripts/export_contracts.py` 生成器刷新共享 `contracts/`。生成器
证明仅以下文件发生变化：

- `openapi/agent-api-v1.yaml`
- `schemas/data/table-data-result.schema.json`
- `schemas/data/tool-specific/event-finish-rate-table.schema.json`
- `schemas/data/tool-specific/housing-area-group-table.schema.json`
- `schemas/data/tool-specific/housing-lease-type-table.schema.json`
- `schemas/data/tool-specific/population-metric-table.schema.json`
- `schemas/tools/tool-result.schema.json`

对新旧生成物递归移除 `title/unit/value_labels` 后逐文件完全一致；
数据值、类型、枚举和 `required` 未变。

## 验证

- TDD RED：住房、人口、事件 4 个新断言均准确复现 logical field、
  无单位、英文层级与 `85.0` 问题。
- 全量 pytest：`734 passed, 18 skipped` (`752 collected`)。
- Ruff：通过。
- Pyright：`0 errors, 0 warnings`。
- Compileall：通过。

## 实际 Event Live

`open-event-finish-rate-query.yaml` 已使用主 `agent-runtime/.env` 实跑，结果
为 **FAIL**，但未进入 claims 渲染：模型第一轮仅获得
`governance.resolve_area + full_view.finish_answer`，解析西湖区后第二轮只剩
`finish_answer`，未获得 `semantic_query/query_event_metrics`。实际 `tool_ids`
仅 `resolve_area`、Evidence 为 1、answer 为 null。该阻塞属于
semantic/catalog/tool-surface，不在本补丁边界内。
