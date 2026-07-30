# P1-G 可信回答事实门禁交付报告

## 交付状态

- 状态：完成，等待 P1-R reviewer 审核。
- 模式：编码智能体 V3.2 `module-dev`，独立 worktree。
- 边界：只修改 P1-G 指定源码、对应测试和本报告；未修改语义入口、
  前端、数据库、凭据、部署门禁、Eval 运行产物或 `coding-assistant/`。

## 实现摘要

1. 建立 Result 事实账本：
   - 记录原始数字、行数、合计、最大值、最小值和可复算占比；
   - 记录区划名、对象标题和未脱敏对象字段；
   - 每项事实保留来源 `result_id`，比较事实也保留来源 Result。
2. 增加确定性事实核验：
   - 拒绝 Result 中不存在的数字、正式区划名和对象名；
   - 校验最大、最小、并列、全部相同等判断；
   - 区分正式区划名与“按社区、几个街道、各区县、全市”等维度词。
3. 稳定完成决策：
   - 原因码包括 `unsupported_number`、`unsupported_area`、
     `unsupported_object`、`unsupported_judgement` 和既有
     `unsupported_inference`；
   - 原因码和修订意见进入下一轮模型上下文；
   - 仅允许一次模型修订；第二版仍不可信时，只采用重新核验通过的
     安全摘录，否则返回固定安全结束语，不再进入无反馈循环。
4. Eval 确定性 Grader：
   - `EvalExpected.grounding_reason_code` 可声明期望事实核验结果；
   - Grader 使用最终落库的 assistant 文本和本轮持久化 Result，
     不依赖 LLM Judge；
   - Native 与 LangGraph 继续复用同一 Harness 完成决策。

## TDD 证据

初始 RED：

- 定向测试出现 5 个预期失败：
  - 区域和对象未校验；
  - 比较判断未校验；
  - 二次修订仍可能继续；
  - 上下文无稳定原因码；
  - Eval 无 grounding grade。

GREEN 后新增覆盖：

- 合法原始值、合计、占比；
- 非法数字；
- 合法与非法区域、对象；
- 最大、最小、并列、全部相同；
- 区划维度词不误报；
- 二次修订失败安全结束；
- 原因码进入模型反馈；
- 确定性 Eval grounding grade。

## Fresh 验证

- 定向测试：
  `uv run pytest tests/test_harness.py tests/test_context_builder.py tests/test_eval_runner.py -q`
  通过。
- 扩展 Gate：
  `uv run pytest tests/test_harness.py tests/test_context_builder.py
  tests/test_eval_runner.py tests/test_eval_suite.py tests/test_eval_cli.py -q`
  通过。
- 全量：`uv run pytest -q` 通过，16 项按既有条件跳过。
- Ruff：`uv run ruff check .`，通过。
- Pyright：`uv run pyright`，0 errors / 0 warnings。
- 编译：`uv run python -m compileall -q src`，通过。

## Reviewer 重点

1. 用真实住房查询复核第一版错误结论触发修订、第二版可信结论完成，
   且成功 Tool 不重复调用。
2. 同一脚本分别跑 Native/LangGraph，确认完成状态、最终安全文本和原因码一致。
3. 当前继承历史 Result 的 Harness 状态只携带已验证
   `result_id/evidence_id`，没有把历史 Result 载荷重新注入完成校验器；
   本任务保持既有“同会话已验证引用可信”契约。若下一阶段要求对历史追问中的
   每个新数字重新复算，需要在 Orchestrator/Store 边界增加历史 Result 水合，
   超出本 worktree 文件边界。
