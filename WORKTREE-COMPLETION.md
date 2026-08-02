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
   - 非 `claims/reference_only` 的结构化类型不能在成功数据后夹带事实正文；
   - 仅允许一次修订，第二次仍缺失或非法时返回固定安全结束语，不触发
     `loop_detected`。
5. Eval：
   - `EvalFinishStep` 保存完整 `structured_finish`；
   - Recording/Scripted provider 可录制和回放 Claim；
   - 回放时按稳定 `result_fingerprint` 将录制 Claim 重新绑定到本次 Result ID；
   - Tool 观察增加 `result_fingerprint`，未增加凭据或内部地址。
6. Prompt 升级为 `full-view-governance-readonly-v13`，明确结构化完成约束。

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
- capability 类型不能在成功结果后夹带事实；
- OpenAI Finish Tool 往返；
- Native/LangGraph 同一结构化 Claim 产生相同事实文本；
- live Eval 录制后 Scripted replay 不丢 Claim。

## Fresh 验证

- 全量测试：`569 passed, 18 skipped`。
- Ruff：`All checks passed!`。
- Pyright：`0 errors, 0 warnings, 0 informations`。
- Compileall：通过。
- Eval：`46/46 pass@1=100%`。

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
