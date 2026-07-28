# SOP-08 · 走查

## 目标
claim 完成前 fresh 跑全量验证，确保代码质量、样式规范、接口契约全部通过。

---

## 前置条件
- SOP-07 自测通过

---

## 输入

> Agent 执行本阶段前，必须先验证以下文件是否存在。
> **输入缺失时，不可直接终止流程。**

| # | 读取路径 | 用途 | 适用场景 | 来源阶段 |
|---|---------|------|---------|---------|
| 1 | `coding-assistant/tools/06_自测报告/自测报告-[日期].md` | 测试结果、覆盖率 | A, C, D, E | SOP-07 |
| 2 | `coding-assistant/tools/04_任务拆分/任务清单-[日期].md` | 每步完成标准（逐条核对） | A, C, D, E | SOP-05 |
| 3 | `coding-assistant/tools/03_接口契约/api.md` 或 `coding-assistant/tools/03_接口契约/openapi.yaml` | 接口字段对照 | A, C, E | SOP-04 |
| 4 | `git diff --name-only main...HEAD` | 改动文件列表 | A, C, D, E | — |

**输入缺失处理：** 任一文件不存在时，Agent 使用 `AskUserQuestion`：

```
问题: [自测报告 / 任务清单 / 接口契约] 未就绪，无法直接执行走查，如何处理？
  [A] 回到上一阶段补充 — 完成前置产出后继续
  [B] 对话引导 — 我现在描述改动范围和验收标准，Agent 直接执行走查并生成报告（标记为"对话生成"）
```

> **场景差异**：C 走查简化（仅构建+测试+字段对照，不查样式规范），D 无 api.md 时不执行接口契约对照。

---

## 核心铁律

> ★ **claim 完成前必须 fresh 跑命令，没运行 = 没通过。**
> "上次跑过"不算数。

---

## 执行步骤

1. **Fresh 跑构建验证**
   - `npm run typecheck`——贴输出
   - `npm run build`——贴输出
   - `npm run lint`——贴输出
   - 任一失败 → 先修复

2. **Fresh 跑全套测试**
   - `npm test`——贴输出
   - 必须全绿

3. **样式规范检查**（套用 `coding-assistant/prompts/P-C-6-代码走查.md`）
   - `grep -r '#[0-9A-F]\{6\}' src/` 检查无硬编码色值
   - `grep -r 'px' src/` 检查无裸 px 间距（排除 token 文件）
   - 人工对照 `docs/样式规范.md` 扫一遍 UI 改动

4. **接口契约对照**（分离架构）
   - 逐字段核对代码实现与 OpenAPI 契约一致

5. **改动范围检查**
   - `git diff --name-only main...HEAD` 确认改动在 CLAUDE.md 文件边界内
   - 改动 ≤ 3 个相关模块

6. **自欺欺人模式检查**
   - 函数体只有 TODO 或空 `{}` → ❌
   - 数据是硬编码 mock → ⚠️
   - import 了但 JSX 未使用 → ❌
   - onClick/回调是空函数 → ❌
   - console.log 替代真实逻辑 → ❌

7. 生成 `coding-assistant/tools/07_走查记录/走查报告-[日期].md`（参照 `coding-assistant/templates/走查报告模板.md`）

---

## 输出

> Agent 完成本阶段后，必须将产出写入以下路径。

| # | 写入路径 | 内容 | 下游阶段 |
|---|---------|------|---------|
| 1 | `coding-assistant/tools/07_走查记录/走查报告-[日期].md` | 构建验证、测试结果、样式规范、契约对照、自欺欺人检查、结论 | SOP-09, SOP-10 |
| 2 | `coding-assistant/tools/99_运行日志/产出索引.md` | 更新 SOP-08 行（状态+产出文件） | — |
| 3 | `coding-assistant/tools/99_运行日志/当前状态.md` | 更新阶段进度 + 下一步 | — |
