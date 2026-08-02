# P1-2 AnalysisPlan 执行器自测

- 定向测试：14 项通过。
- 全量测试：766 项收集，收口命令 exit 0（现有依赖弃用警告未新增）。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- `uv run python -m compileall -q src`：通过。
- 覆盖：并发上限、依赖顺序、partial、单步/总超时、外部取消、
  重新认证控制流、Catalog version/fingerprint 漂移、capability binding 漂移、
  Resolver 错接、输出顺序、Tool 调用预算和过长工具摘要。
