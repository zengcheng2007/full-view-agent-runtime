# SOP-05 · 任务拆分

## 目标
按业务闭环将技术方案拆分为可执行的编码步骤，每步 2~5 分钟一个 commit 粒度。

---

## 前置条件
- SOP-04 接口契约已就位
- 分离架构下契约 PR 已合

---

## 输入

> Agent 执行本阶段前，必须先验证以下文件是否存在。
> **输入缺失时，不可直接终止流程。**

| # | 读取路径 | 用途 | 适用场景 | 来源阶段 |
|---|---------|------|---------|---------|
| 1 | `coding-assistant/tools/02_技术方案/技术方案-[日期]-[feature].md` | 任务拆分草案（§6） | A, E | SOP-03 |
| 2 | `coding-assistant/tools/03_接口契约/api.md` 或 `coding-assistant/tools/03_接口契约/openapi.yaml` | 接口字段约束 | A, C, E | SOP-04 |

**输入缺失处理：** 任一文件不存在时，Agent 使用 `AskUserQuestion`：

```
问题: [技术方案 / 接口契约] 未就绪，无法直接拆分任务，如何处理？
  [A] 回到上一阶段补充 — 完成前置产出后继续
  [B] 对话引导 — 我现在描述要做的事情，Agent 直接生成任务清单（标记为"对话生成"）
```

用户选择 [B] 时，Agent 通过对话了解功能点，按业务闭环拆分为 2~5 分钟粒度的步骤，生成后标注"对话生成，待确认"。

> **场景差异**：A-前端 无技术方案时读设计稿代替，C 无技术方案时读 OpenAPI 代替，D 无技术方案和 api.md，以原型+JSON数据映射表代替拆分依据。详见 `coding-assistant/sop/场景路由表.md`。

---

## 拆分原则

- 每步 2~5 分钟一个 commit 粒度
- **按业务闭环切，不按文件切**——一个步骤完成一个可验证的用户可感知行为
- 每步含 [测试 → 实现 → 验证 → 提交] 四个动作
- 步骤间依赖关系标注清楚

---

## 执行步骤

1. 读取 `coding-assistant/tools/02_技术方案/` 中的任务拆分草案
2. 读取 `coding-assistant/tools/03_接口契约/` 确认接口字段
3. 激活 skill：`Use the writing-plans skill`
4. 套用 `coding-assistant/prompts/P-C-5-任务拆分.md` 细化任务
5. **自动判定多Agent 模式**：统计任务清单涉及的文件数和模块数：
   - ≤3 文件且 ≤1 模块 → **场景一**（单 Agent TDD），写入 `coding-assistant/tools/04_任务拆分/多Agent判定.yaml`：`mode: single`
   - ≥4 文件或 ≥2 模块 → **场景二候选**（orchestrator 编排），继续执行 worktree 适用性分析（§5a），写入 `mode: multi` 或 `mode: worktree`
   - 不确定 → `mode: single`，标注 `fallback: multi_if_stuck`

### 5a. worktree 适用性分析（仅场景二候选时执行）

当任务清单含 ≥2 个独立模块时，分析是否适合 worktree 物理并行：

**判据（全部满足 → worktree 模式）：**

| 判据 | 检查方式 |
|------|---------|
| 子任务文件范围互斥 | 每个子任务的文件列表做交集，交集必须为空 |
| 无步骤间强依赖 | 依赖关系中无 `Step N → Step M` 跨模块依赖 |
| 每个子任务可独立验证 | 每个子任务的完成标准不依赖其他子任务的代码 |
| 非分离架构下的前后端耦合 | 全栈项目的前后端子任务不可 worktree 并行（共享契约） |

**判定结果：**

```
全部满足 → mode: worktree
          在 多Agent判定.yaml 中为每个子任务填写 file_ranges（路径列表）
          在 SOP-06 将进入「场景三：worktree 并行编码」
任一条不满足 → mode: multi
                在 SOP-06 将进入「场景二：多 Agent 编排」
```

**worktree 拆分粒度调整：** worktree 模式的子任务粒度比场景一/二更大——每个 worktree 应包含 3~8 个 TDD 步骤，独立完成一个业务闭环（如"用户管理模块"而非"用户类型定义"）。

6. 输出到 `coding-assistant/tools/04_任务拆分/任务清单-[日期].md`
7. 输出到 `coding-assistant/tools/04_任务拆分/多Agent判定.yaml`（格式见 §输出）
8. **停下来**——告知用户审核任务清单和多Agent判定。如果 mode = worktree，告知用户并行方案和预计提速

---

## 输出

> Agent 完成本阶段后，必须将产出写入以下路径。

| # | 写入路径 | 内容 | 下游阶段 |
|---|---------|------|---------|
| 1 | `coding-assistant/tools/04_任务拆分/任务清单-[日期].md` | 每步目标、涉及文件、完成标准、TDD 四动作、依赖关系 | SOP-06, SOP-10 |
| 2 | `coding-assistant/tools/04_任务拆分/多Agent判定.yaml` | mode + 判定依据（见下方格式） | SOP-06 |
| 3 | `coding-assistant/tools/99_运行日志/产出索引.md` | 更新 SOP-05 行（状态+产出文件） | — |
| 4 | `coding-assistant/tools/99_运行日志/当前状态.md` | 更新阶段进度 + 下一步 | — |

### 多Agent判定.yaml 格式

**mode = single:**
```yaml
mode: single
file_count: 3
module_count: 1
fallback: multi_if_stuck
```

**mode = multi:**
```yaml
mode: multi
file_count: 7
module_count: 2
reason: "跨模块依赖，无法拆分为独立 worktree"
parallel: false
```

**mode = worktree:**
```yaml
mode: worktree
worktree_count: 3
estimated_speedup: "2~3x"

# 批次：同 batch 的 worktree 必须在多个终端中同时启动
# 批次间串行：batch 2 等 batch 1 全部合并后再开始
batches:
  - id: 1
    parallel: true
    worktrees:
      - name: user-module
        files:
          readWrite:
            - "src/components/user/"
            - "src/services/user/"
            - "src/stores/user/"
            - "src/types/user/"
          readOnly:
            - "src/types/"
            - "src/stores/"
            - "src/tokens.ts"
            - "coding-assistant/tools/03_接口契约/"
        steps: 5
        depends_on: []

      - name: order-module
        files:
          readWrite:
            - "src/components/order/"
            - "src/services/order/"
            - "src/stores/order/"
            - "src/types/order/"
          readOnly:
            - "src/types/"
            - "src/stores/"
            - "src/tokens.ts"
            - "coding-assistant/tools/03_接口契约/"
        steps: 4
        depends_on: []

  - id: 2
    parallel: false
    wait_for: ["user-module"]
    worktrees:
      - name: payment-module
        files:
          readWrite:
            - "src/components/payment/"
            - "src/services/payment/"
            - "src/types/payment/"
          readOnly:
            - "src/types/"
            - "src/stores/"
            - "src/tokens.ts"
            - "coding-assistant/tools/03_接口契约/"
        steps: 3
        depends_on: ["user-module"]
```

---

## 质量要求

- 每步目标一句话说清
- 涉及文件精确到路径
- 完成标准可验证（命令或行为）
- TDD 流程每步四个动作明确

> ★ **Plan 写完研发先扫一遍**，别让 AI 编步骤——你才知道哪些能合并。
