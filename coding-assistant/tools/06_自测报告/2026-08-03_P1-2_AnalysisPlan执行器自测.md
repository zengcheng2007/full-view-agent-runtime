# P1-2 AnalysisPlan 执行器自测

- 定向测试：31 项通过。
- 全量测试：783 项收集，收口命令 exit 0（现有依赖弃用警告未新增）。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- `uv run python -m compileall -q src`：通过。
- 覆盖：并发上限、依赖顺序、partial、单步/总超时、外部取消、
  重新认证控制流、Catalog version/fingerprint 漂移、capability binding 漂移、
  Resolver 错接、输出顺序、Tool 调用预算和过长工具摘要。
- Reviewer 退回后新增攻击型覆盖：旧 plan ID 篡改 scope、重算 ID 后跨 scope、
  重复 step ID、cycle、steps 超 budget、`model_construct` 污染、goal/subject 错配、
  不可推导语义参数的零调用拒绝。
- 第二轮新增：公共端口无 Plan 正文、repo not found、repo 返回错 ID、
  request_id 错绑、overview 缺主题、伪造 omission、整体换 scope 仍用旧 ID，
  以及同 plan_id 跨 tenant/user/run 命名空间不可见。全部均验证零 Tool 调用。
