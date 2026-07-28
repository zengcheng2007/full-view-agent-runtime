# SOP-06 · 编码

## 目标
TDD 红绿循环，按任务清单逐步实现，每步产生可追溯的 commit。

---

## 前置条件
- SOP-05 任务拆分完成，人已审核
- 文件边界已在 `coding-assistant/tools/00_项目配置/项目信息.md` 中明确

---

## 输入

> Agent 执行本阶段前，必须先验证以下文件是否存在。
> **输入缺失时，不可直接终止流程。**

| # | 读取路径 | 用途 | 适用场景 | 来源阶段 |
|---|---------|------|---------|---------|
| 1 | `coding-assistant/tools/04_任务拆分/任务清单-[日期].md` | 编码任务：每步目标、文件范围、完成标准 | A, C, D, E | SOP-05 |
| 2 | `coding-assistant/tools/03_接口契约/api.md` 或 `coding-assistant/tools/03_接口契约/openapi.yaml` | 接口字段约束 | A, C, E | SOP-04 |
| 3 | `coding-assistant/tools/00_项目配置/项目信息.md` | 文件边界三区 | A, C, D, E | SOP-01 |
| 4 | `coding-assistant/tools/01_需求输入/设计稿解析-[日期].md` | 设计稿数据源类型 + tokens + 组件树 + 不确定项 | **A-前端(必需)** | 本阶段 §场景 A-前端 预编码门禁 |

**输入缺失处理：** 任一文件不存在时，Agent 使用 `AskUserQuestion`：

```
问题: [任务清单 / 接口契约] 未就绪，无法直接开始编码，如何处理？
  [A] 回到上一阶段补充 — 完成前置产出后继续
  [B] 对话引导 — 我现在描述要做什么，Agent 直接按 TDD 编码（标记为"对话生成"）
```

用户选择 [B] 时，Agent 通过对话确认本次编码的目标、文件范围、完成标准，然后按 TDD 红绿循环编码。涉及接口字段时由用户口述或 Agent 推测并确认。

> **场景差异**：D 无 api.md，以 `coding-assistant/tools/03_接口契约/` 下的 JSON 数据文件作为"数据契约"代替接口契约。

---

## 场景 A-前端 预编码门禁

> **仅场景 A-前端 触发。** 其余场景/子类型跳过本节,直接进入下方"场景判据"。
>
> 场景 A-前端 的代码质量取决于设计稿数据源,**编码前必须先确认数据源类型(MCP 直读 / 截图参照)**,否则后续 token / 间距 / 组件结构靠猜,SOP-08 走查必返工。

### 触发条件

[当前状态.md](coding-assistant/tools/99_运行日志/当前状态.md) 中 `场景 = A` 且 `子类型 = 前端`，且 `数据源类型` 字段为空或未确认。

### 执行步骤

Agent 套用 [P-C-B-设计稿读取确认.md](coding-assistant/prompts/P-C-B-设计稿读取确认.md),按 6 步交互流程逐项确认:

```
Step 1: 确认是否有前端原型设计稿链接? (AskUserQuestion: 有链接 / 仅截图 / 都没有)
   ├─ 有链接 → Step 2
   ├─ 仅截图 → 跳 Step 6
   └─ 都没有 → 终止,要求补充输入或切换场景 A

Step 2: 识别设计稿平台类型(Calicat / MasterGo / Figma / Sketch / Pixso)
        → Agent 解析链接域名自动识别,无法识别时 AskUserQuestion 让用户选

Step 3: 查询对应 MCP 可用性 (claude mcp list)
   ├─ MCP 已装 → 跳 Step 5
   ├─ MCP 未装但存在 → Step 4
   └─ 平台无 MCP → 跳 Step 6 (告知用户走截图模式)

Step 4: 询问是否安装 MCP (AskUserQuestion: 安装 / 跳过改截图)
   ├─ 安装 → 执行安装命令 → 提示用户重启 Claude Code → 等用户回"继续" → Step 5
   └─ 跳过 → Step 6

Step 5: 用户授权 MCP,Agent 调用 MCP 读取设计稿
        → 提取 tokens / 组件树 / 截图 → 输出"设计稿解析报告"
        → 数据源类型标记为"MCP 直读" → 进入 Step 7

Step 6: 截图参照模式(降级)
        → 用户放截图到 coding-assistant/tools/01_需求输入/设计稿截图/
        → Agent 多模态读图 → 推断 tokens → 列不确定项
        → 数据源类型标记为"截图参照" → 进入 Step 7

Step 7: 进入下方"场景判据"开始 TDD 编码
```

### 平台与 MCP 对应表

| 平台 | 识别特征 | 对应 MCP | 备注 |
|------|---------|---------|------|
| Calicat | `*.calicat.*` | `calicat` MCP(本工程内置) | 工具集已内置,无需安装 |
| MasterGo | `mastergo.com` | `mastergo` MCP | 需 `claude mcp add` 安装 |
| Figma | `figma.com/file/*` 或 `figma.com/design/*` | `figma-developer-mcp` / `figma-context-mcp` | 需安装 |
| Sketch | `.sketch` 本地文件 | 暂无成熟 MCP(2026-05) | 走截图参照 |
| Pixso | `pixso.cn` / `pixso.net` | 暂无官方 MCP(2026-05) | 走截图参照 |

> Agent 执行 Step 3 时,以 `claude mcp list` 实际结果为准,本表仅作平台匹配参考。

### 数据源差异对编码的影响

| 数据源 | token 来源 | 间距精度 | 组件结构 | SOP-08 走查重点 |
|--------|-----------|---------|---------|----------------|
| **MCP 直读** | 设计稿原值,精确 | 像素级精确 | 完整图层树 | 整体视觉对照即可 |
| **截图参照** | 取色器估值 + 多模态推断 | 肉眼估算,可能偏 1~4px | 仅能识别可见层级 | **每个 token 逐项核对原稿** |

### 红线

- 场景 A-前端 未完成 Step 1~6 之前,**禁止进入 TDD 编码**
- MCP 安装命令参数不得猜测,以官方文档或用户提供为准
- 截图模式下生成的 token 必须在 SOP-08 走查时人工核对原稿
- MCP 调用失败不可自动降级到"瞎写",必须显式询问用户走截图路径

---

## 场景判据（执行前判断）

编码阶段执行前，先读取 `coding-assistant/tools/04_任务拆分/多Agent判定.yaml`，根据 mode 选择执行路径：

```
mode: single    → 场景一：主 Agent 直接执行 TDD
mode: multi     → 场景二：启动 orchestrator 编排
mode: worktree  → 场景三：worktree 物理并行编码
不确定           → 先按场景一试，跑不下去切场景二
```

---

## 场景一：单 Agent TDD 循环（默认）

对任务清单中每个 Step 循环：

1. **读任务**：确认目标文件在 CLAUDE.md 文件边界可读写列表内
2. **激活 skill**：输入 `Use the test-driven-development skill`
3. **红**：先写失败测试
4. **跑测试看到红**：贴测试失败输出
5. **绿**：写最少代码让测试通过
6. **跑测试看到绿**：贴测试通过输出
7. **提交**：`git commit -m "feat(module): step N description"`
8. **记录**：更新 `coding-assistant/tools/99_运行日志/当前状态.md` 和 `coding-assistant/tools/05_编码产物/`

---

## 场景二：多 Agent 编排（复杂任务）

当任务跨 4+ 文件或跨多个模块时，启用 orchestrator：

### 启动 orchestrator

```
Use the 编码-Agent:orchestrator

任务描述：[从 coding-assistant/tools/04_任务拆分/ 中获取当前功能的任务清单]
文件边界：[从 CLAUDE.md 第五章复制]
验收命令：[从 CLAUDE.md 第八章复制]

请将任务拆解为文件级子任务，并路由给执行 agent。
```

### orchestrator 执行流程

```
1. orchestrator 拆解任务 → N 个子任务（每步含：目标/文件范围/完成标准/禁改文件）
2. orchestrator 按路由表派发子任务：
   - 有 OMC → oh-my-claudecode:executor（model=sonnet/opus 按复杂度选）
   - 无 OMC → 编码-Agent:module-dev
3. 每个执行 agent 独立完成子任务（TDD 红绿循环）
4. 执行 agent 完成后向 orchestrator 报告
5. orchestrator 调用 reviewer 验收
6. reviewer 输出验收报告
7. orchestrator 汇总结果，告知主 Agent
```

### reviewer 验收

```
Use the 编码-Agent:reviewer

验收任务：[功能名称]
完成标准：[从任务清单获取]
文件边界：[从 CLAUDE.md 复制]
验收命令：[从 CLAUDE.md 第八章复制]
改动文件列表：[git diff --name-only]

请执行验收并输出报告。
```

---

## 场景三：Worktree 物理并行编码

当 `多Agent判定.yaml` 中 `mode: worktree` 时，编码在**多个独立的 git worktree** 中并行执行。

### 为什么用 worktree 而不是场景二

| | 场景二（多 Agent 编排） | 场景三（worktree 并行） |
|------|------|------|
| 隔离方式 | prompt 声明文件边界 | git worktree 物理目录隔离 |
| Context | 共享主 session context | 每个 worktree 独立 context 窗口 |
| Agent 执行 | 串行调度（A 完→B 完→C 完） | 真正并行（A/B/C 同时跑） |
| 适用 | 有跨模块依赖、需统一协调 | 子任务文件范围互斥、可独立验证 |

### 前置条件

- `多Agent判定.yaml` 中 mode = worktree
- 每个 worktree 的 `files.readWrite` 已填写
- 主项目的 `.claude/settings.json` 有 PreToolUse hook（`check-protected-files.mjs`）
- 用户有足够终端窗口（每个 worktree 一个独立 Claude Code session）

### 执行流程

worktree 并行编码的执行分为**三个阶段**，其中核心编码阶段（阶段二）是真正并行的：

```
阶段一（串行，Agent 完成）    阶段二（N 路并行，人执行）        阶段三（串行）
┌─────────────────────┐     ┌─────────────────────────┐     ┌──────────────────┐
│ Agent 输出并行方案    │     │ ▸ 终端 1: worktree A    │     │ merge-worktrees  │
│ + 检查清单            │ ──→ │ ▸ 终端 2: worktree B    │ ──→ │ 冲突解决          │
│ + init-worktree 命令  │     │ ▸ 终端 3: worktree C    │     │ → SOP-07 自测     │
└─────────────────────┘     │   ← 同时进行，不等待 →   │     └──────────────────┘
                            └─────────────────────────┘
```

#### 阶段一：Agent 输出并行方案（串行，一次完成）

1. 读取 `coding-assistant/tools/04_任务拆分/多Agent判定.yaml`
2. 根据 `worktrees[].depends_on` 分组为批次：
   - 批次 1：`depends_on: []` 的所有 worktree
   - 批次 2：依赖批次 1 中某个 worktree 的（如有）
   - 批次 N：以此类推
3. 按 orchestrator 的输出格式（`coding-assistant/agents/orchestrator.md` §worktree 模式）输出方案：

```
═══════════════════════════════════════
 ⚡ 批次 1：同时创建 3 个 worktree
   现在打开 3 个终端窗口，在每个终端中执行对应命令。
   ⛔ 不要等一个完成再执行下一个——这是并行的价值。
═══════════════════════════════════════

▸ 终端 A — user-module ─────────────────────
bash coding-assistant/scripts/init-worktree.sh user-module \
  "src/components/user/" "src/services/user/" "src/stores/user/" "src/types/user/"
cd <项目>-user-module && claude
进入后说：开始编码。任务：...
────────────────────────────────────────────

▸ 终端 B — order-module ────────────────────
bash coding-assistant/scripts/init-worktree.sh order-module ...
cd <项目>-order-module && claude
进入后说：开始编码。任务：...
────────────────────────────────────────────

▸ 终端 C — payment-module ──────────────────
  ⚠️ 此 worktree 依赖 user-module，等批次 1 合并后再执行
bash coding-assistant/scripts/init-worktree.sh payment-module ...
────────────────────────────────────────────
```

4. 输出并行执行检查清单：

```
## 🎯 并行执行检查清单

  ☐ 同批次的 N 个 worktree 必须同时启动（多个终端窗口）
  ☐ 不能等 A 完成再启动 B——那不是并行，是串行
  ☐ 我已有 N 个可用的终端窗口
  ☐ 全部编码完成后回到主项目运行 merge-worktrees.sh

确认后开始执行批次 1。
```

5. **停下来** —— 等人执行完所有 worktree 并告知完成后，再进入阶段三。

#### 阶段二：人并行执行（N 路同时进行）

这是 worktree 模式的核心——人在多个终端窗口中**同时**操作：

```
终端 1                      终端 2                      终端 3
┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│ cd <项目>-user    │       │ cd <项目>-order   │       │ (等待批次 1 合并)  │
│ claude            │       │ claude            │       │                  │
│ → 开始编码         │       │ → 开始编码         │       │                  │
│ → TDD 红绿循环     │       │ → TDD 红绿循环     │       │                  │
│ → 每步 commit     │       │ → 每步 commit     │       │                  │
│ → 输出完成报告     │       │ → 输出完成报告     │       │                  │
└──────────────────┘       └──────────────────┘       └──────────────────┘
        ↓                          ↓                          ↓
   回到主项目 ───────── merge-worktrees.sh ──────────── 回到主项目
```

每个 worktree 的 CLAUDE.md 已预置：
- 文件边界（只允许改该子任务的目录）
- 验收命令（从主项目继承）
- PreToolUse hook（check-protected-files.mjs 自动生效）

Agent 在 worktree 中只做 TDD 编码，不走 SOP 流程。

#### 阶段三：合并（串行）

人在**主项目目录**执行（所有 worktree 编码完成后）：

```bash
cd <主项目>
bash coding-assistant/scripts/merge-worktrees.sh user-module order-module
```

合并完成后：
- 如有下一批次 → 人执行批次 2 的 init-worktree（阶段二重复）
- 无下一批次 → 进入「日志回填」

#### 日志回填（Agent 自动执行）

合并回到主项目后，Agent 必须先恢复上下文再进入 SOP-07：

1. **读取所有完成报告**：扫描 `coding-assistant/tools/05_编码产物/WORKTREE-COMPLETION-*.md`
2. **生成编码索引**：汇总所有完成报告，生成 `coding-assistant/tools/05_编码产物/编码索引-[日期].md`
3. **更新运行日志**：
   - `coding-assistant/tools/99_运行日志/当前状态.md`：SOP-06 状态 → ✅ 完成，写入每个 worktree 的改动摘要
   - `coding-assistant/tools/99_运行日志/执行记录.md`：追加 worktree 并行编码记录
   - `coding-assistant/tools/99_运行日志/产出索引.md`：更新 SOP-06 行
4. **清理**：删除 `WORKTREE-COMPLETION-*.md` 临时文件（信息已汇入编码索引）
5. **汇报摘要**：

```
## Worktree 并行编码完成

| Worktree | 改动文件数 | 验收 | 遗留问题 |
|----------|-----------|------|---------|
| user-module | 5 | ✅ | 无 |
| order-module | 4 | ✅ | 无 |
| payment-module | 3 | ✅ | 无 |

SOP-06 完成。下一步：SOP-07 自测。
```

### 注意事项

- **worktree 中的 Agent 不生成编码索引**，只生成 WORKTREE-COMPLETION.md，由主项目 Agent 汇总
- **merge-worktrees.sh 自动保留完成报告**到 `coding-assistant/tools/05_编码产物/`，worktree 删除前会复制
- **完成报告缺失时**：Agent 无法自动恢复上下文，必须问用户索要改动摘要

- **不要在 worktree 中 push**——合并由主项目完成
- **有依赖的 worktree** 等前置 worktree 合并后再开始
- **冲突处理**：如果多个 worktree 改了同一个文件，merge-worktrees.sh 会报冲突，人手动解决
- **worktree 的 CLAUDE.md 是精简版**——只有单任务上下文，不走 SOP 流程
- **禁止在 worktree 中修改** `coding-assistant/`、`.claude/`、`CLAUDE.md`

### 与 MAC 的对比

本模式参考了 [multi-agent-coding](https://github.com/AllenTang-AI/multi-agent-coding) 的 worktree 机制，差异如下：

| | MAC | V3.1 worktree 模式 |
|------|-----|------|
| 触发方式 | 人用 `/orchestrate` 拆模块后手动 `mac-new.sh` | SOP-05 自动判定 + Agent 输出命令 |
| 粒度 | 功能模块级 | 子任务级（更细） |
| 生命周期 | 完整 feature 分支 | 编码阶段临时分支，合并即删除 |
| 知识库 | 有 knowledge/ + promote 机制 | 无（回到主项目在 SOP-10 沉淀） |
| CLAUDE.md | 完整上下文（agent 路由 + context 注入） | 精简版（单任务 + 文件边界 + 验收命令） |

---

## 编码约束（场景一/场景二/场景三通用）

- [ ] 色值/间距/圆角/字号 `import from '@/tokens'`（非 tokens.ts 文件）
- [ ] 命名：组件 PascalCase / 函数 camelCase / 文件 kebab-case
- [ ] 不引入新的无关抽象
- [ ] 不重构无关代码
- [ ] 不吞异常（不要防御性 try/catch）
- [ ] 不写 TODO 注释——要么做要么删
- [ ] 不在 CLAUDE.md「禁止修改」列表的文件中写入

---

## 异常处理

### 同一错误修了 3 次还没过
停下来，写入待确认问题清单，告知用户：
```
[功能名称] Step N 遇到阻塞：
错误：[报错信息]
已尝试：[3次尝试记录]
需要：[用户帮助判断]
```

### 遇到业务/设计不确定
立刻问用户，不猜。格式：
```
[功能名称] Step N 需要确认：
问题：[一句话]
背景：[为什么需要这个信息]
选项 A / 选项 B：[方案及影响]
```

### 执行 agent 越界写入
pre-tool-use hook 自动拦截。如发现 agent 尝试修改禁止修改的文件，停止该 agent，报告 orchestrator。

### 回退策略

同一错误 3 次或发现方向性错误时，不回退到上一个 Step：

```
# 回退到 Step N 之前的状态（保留工作区未提交修改）
git stash
git reset --hard <Step N-1 的 commit>

# 或只回退单个文件
git checkout <commit> -- <file>
```

**原则：**
- 小范围回退（1~2 文件）→ `git checkout <commit> -- <file>`
- 大范围回退（整个 Step 方向错了）→ `git reset --hard` 回到上一个正确 commit
- 回退前如果有未提交的临时修改 → 先 `git stash`
- 回退后告知用户原因和回退范围

---

## 输出

> Agent 完成本阶段后，必须将产出写入以下路径。

| # | 写入路径 | 内容 | 下游阶段 |
|---|---------|------|---------|
| 1 | `src/`（按文件边界） | 可运行代码 | SOP-07 |
| 2 | `coding-assistant/tools/05_编码产物/编码索引-[日期].md` | 改动文件清单、Step 完成情况、commit 记录 | SOP-07, SOP-10 |
| 3 | `coding-assistant/tools/99_运行日志/当前状态.md` | 每步完成状态更新 | — |
| 4 | 待确认问题清单（如有）| 写入 `coding-assistant/tools/99_运行日志/当前状态.md` 阻塞项 | — |
| 5 | `coding-assistant/tools/99_运行日志/产出索引.md` | 更新 SOP-06 行（状态+产出文件） | — |
| 6 | `coding-assistant/tools/99_运行日志/当前状态.md` | 更新阶段进度 + 当前Step | — |
| 7 | `coding-assistant/tools/01_需求输入/设计稿解析-[日期].md` | **场景 A-前端 专属**：数据源类型(MCP直读/截图参照) + tokens + 组件树 + 不确定项 | SOP-08 |

编码索引格式：

```
# 编码索引

> 日期：YYYY-MM-DD
> 任务清单：coding-assistant/tools/04_任务拆分/任务清单-[日期].md

## 改动文件
| 文件路径 | 操作（新建/修改） | 对应 Step |
|---------|-----------------|----------|
| src/components/xxx.tsx | 新建 | Step 3 |
| src/services/xxx.ts | 修改 | Step 1 |

## Step 完成情况
| Step | 状态 | Commit | 备注 |
|------|------|--------|------|
| Step 1 | ✅ | abc1234 | feat(module): add types |
| Step 2 | ✅ | def5678 | feat(module): add service |

## 验收情况（场景二）
- reviewer 验收报告：[路径]
```
