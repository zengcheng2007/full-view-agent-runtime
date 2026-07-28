#!/bin/bash
# init-worktree.sh — 为一个编码子任务创建隔离的 git worktree
#
# 用法: bash coding-assistant/scripts/init-worktree.sh <task-name> [file-ranges...]
#
# 示例: bash coding-assistant/scripts/init-worktree.sh user-list \
#         "src/components/user/" "src/services/user/" "src/types/user/"
#
# 执行后:
#   - git worktree add <项目>-<task> -b feature/<task>
#   - 生成精简版 CLAUDE.md（仅含该任务的文件边界和完成标准）
#   - 从主项目继承 .claude/settings.json 和 check-protected-files.mjs
#   - 不复制 coding-assistant/（worktree 只做编码，不走完整 SOP）

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"

if [ -z "$PROJECT_ROOT" ]; then
    echo "❌ 不在 git 仓库中"
    exit 1
fi

TASK_NAME="${1:?用法: bash coding-assistant/scripts/init-worktree.sh <task-name> [file-ranges...]}"
shift
FILE_RANGES=("$@")

# ── 读取项目配置 ─────────────────────────────────────────────────────────────
PROJECT_INFO="$PROJECT_ROOT/coding-assistant/tools/00_项目配置/项目信息.md"
TASK_LIST_DIR="$PROJECT_ROOT/coding-assistant/tools/04_任务拆分"

# 从项目信息中提取验收命令（按端型）
detect_verify_commands() {
    if [ -f "$PROJECT_INFO" ]; then
        local content
        content="$(cat "$PROJECT_INFO")"
        if echo "$content" | grep -q "Python"; then
            TYPE_CHECK="uv run python -m compileall -q src"
            BUILD_CMD="uv run pytest"
            LINT_CMD="uv run ruff check ."
            TEST_CMD="uv run pyright && uv run pytest"
        elif echo "$content" | grep -q "前端"; then
            TYPE_CHECK="pnpm exec tsc --noEmit"
            BUILD_CMD="pnpm run build"
            LINT_CMD="pnpm run lint"
            TEST_CMD="pnpm run test"
        elif echo "$content" | grep -q "后端"; then
            TYPE_CHECK="mvn compile -q"
            BUILD_CMD="mvn package -DskipTests"
            LINT_CMD="mvn checkstyle:check"
            TEST_CMD="mvn test"
        else
            TYPE_CHECK="pnpm exec tsc --noEmit"
            BUILD_CMD="pnpm run build"
            LINT_CMD="pnpm run lint"
            TEST_CMD="pnpm run test"
        fi
    else
        TYPE_CHECK="pnpm exec tsc --noEmit"
        BUILD_CMD="pnpm run build"
        LINT_CMD="pnpm run lint"
        TEST_CMD="pnpm run test"
    fi
}

# 从项目信息中提取全局只读/禁止修改区域
detect_global_boundaries() {
    GLOBAL_RO=""
    GLOBAL_FORBIDDEN=""
    if [ -f "$PROJECT_INFO" ]; then
        GLOBAL_RO=$(python3 -c "
import re, sys
content = open('$PROJECT_INFO').read()
# 提取只读区域
ro_match = re.search(r'###?\s*(?:前端|后端)?\s*[—\-]?\s*可只读(.*?)(?=###|$)', content, re.DOTALL)
if ro_match:
    lines = re.findall(r'^[\s]*[-*]\s*[`\"]?([^`\"\n]+)', ro_match.group(1), re.MULTILINE)
    print('\n'.join(l.strip() for l in lines))
" 2>/dev/null || echo "")
        GLOBAL_FORBIDDEN=$(python3 -c "
import re, sys
content = open('$PROJECT_INFO').read()
fb_match = re.search(r'###?\s*(?:前端|后端)?\s*[—\-]?\s*禁止修改(.*?)(?=###|$)', content, re.DOTALL)
if fb_match:
    lines = re.findall(r'^[\s]*[-*]\s*[`\"]?([^`\"\n]+)', fb_match.group(1), re.MULTILINE)
    print('\n'.join(l.strip() for l in lines))
" 2>/dev/null || echo "")
    fi
}

detect_verify_commands
detect_global_boundaries

# ── 创建 worktree ────────────────────────────────────────────────────────────
WORKTREE_DIR="${PROJECT_ROOT}-${TASK_NAME}"
BRANCH="feature/${TASK_NAME}"

if [ -d "$WORKTREE_DIR" ]; then
    echo "❌ worktree 已存在: $WORKTREE_DIR"
    echo "   如需重建: cd $PROJECT_ROOT && git worktree remove $WORKTREE_DIR"
    exit 1
fi

cd "$PROJECT_ROOT"
git worktree add "$WORKTREE_DIR" -b "$BRANCH"

echo "🌿 worktree 就绪: $WORKTREE_DIR (branch: $BRANCH)"

# ── 创建目录结构 ─────────────────────────────────────────────────────────────
mkdir -p "$WORKTREE_DIR/.claude"

# ── 继承主项目的 hook 配置和脚本 ──────────────────────────────────────────────
# 复制 .claude/settings.json（含 PreToolUse hook 用于文件边界保护）
if [ -f "$PROJECT_ROOT/.claude/settings.json" ]; then
    cp "$PROJECT_ROOT/.claude/settings.json" "$WORKTREE_DIR/.claude/settings.json"
fi

# 复制文件边界保护脚本
if [ -f "$PROJECT_ROOT/coding-assistant/scripts/check-protected-files.mjs" ]; then
    mkdir -p "$WORKTREE_DIR/coding-assistant/scripts"
    cp "$PROJECT_ROOT/coding-assistant/scripts/check-protected-files.mjs" \
       "$WORKTREE_DIR/coding-assistant/scripts/check-protected-files.mjs"
fi

# ── 构建文件边界列表 ─────────────────────────────────────────────────────────
RW_LIST=""
for range in "${FILE_RANGES[@]}"; do
    RW_LIST="${RW_LIST}- \`${range}\`"$'\n'
done
RW_LIST="${RW_LIST}- \`WORKTREE-COMPLETION.md\`"$'\n'

# 全局只读
RO_CONTENT=""
if [ -n "$GLOBAL_RO" ]; then
    while IFS= read -r line; do
        [ -n "$line" ] && RO_CONTENT="${RO_CONTENT}- \`${line}\`"$'\n'
    done <<< "$GLOBAL_RO"
else
    RO_CONTENT="- \`src/types/\`（全局类型）
- \`src/tokens.ts\`（设计 token）
- \`coding-assistant/tools/03_接口契约/\`"
fi

# 全局禁止修改
FORBIDDEN_CONTENT=""
if [ -n "$GLOBAL_FORBIDDEN" ]; then
    while IFS= read -r line; do
        [ -n "$line" ] && FORBIDDEN_CONTENT="${FORBIDDEN_CONTENT}- \`${line}\`"$'\n'
    done <<< "$GLOBAL_FORBIDDEN"
else
    FORBIDDEN_CONTENT="- \`CLAUDE.md\`
- \`.claude/settings.json\`
- \`coding-assistant/\`（SOP/Agent/Prompt 定义）
- 其他模块的 \`src/\` 文件"
fi

# ── 生成 worktree CLAUDE.md ──────────────────────────────────────────────────
cat > "$WORKTREE_DIR/CLAUDE.md" << CLAUDEEOF
# Worktree 编码：${TASK_NAME}

## 一、身份

你是编码执行 Agent，当前在隔离 worktree 中实现单个子任务。

**工作模式：** TDD 红绿循环，每步 commit。完成后通知人审阅。

**你不走完整 SOP 流程。** 你只做一件事：按完成标准把代码写出来。

---

## 二、当前任务

> 来自主项目的任务清单：\`coding-assistant/tools/04_任务拆分/\`

**任务名：** ${TASK_NAME}

**完成标准：** [从主项目任务清单读取，由 orchestrator 传入]

**改动文件范围：**
${RW_LIST}

---

## 三、文件边界（最高优先级）

**可读写（你只能改这些）：**
${RW_LIST}
**可只读（理解上下文，不能改）：**
${RO_CONTENT}
**禁止修改：**
${FORBIDDEN_CONTENT}

> pre-tool-use hook 在操作系统层面拦截越界写入。不在可读写列表的文件会被自动阻止。

---

## 四、验收命令

\`\`\`bash
${TYPE_CHECK}
${BUILD_CMD}
${LINT_CMD}
${TEST_CMD}
\`\`\`

---

## 五、编码约束

- [ ] TDD 红绿循环：先写测试 → 看到红 → 最少代码变绿 → commit
- [ ] 每步 2~5 分钟粒度，按业务闭环切
- [ ] 色值/间距/圆角/字号从 tokens 导入（前端）
- [ ] Controller 不含业务逻辑，Entity 不含业务逻辑（后端）
- [ ] 不写 TODO，不吞异常，不引入无关抽象
- [ ] 同一错误 3 次 → 停下来，写原因，回到主项目问人

---

## 六、完成后的操作

编码完成并通过全部验收命令后：

1. **输出完成报告到文件**：在 worktree 根目录创建 \`WORKTREE-COMPLETION.md\`（格式见下方），这是主项目恢复上下文和更新运行日志的唯一数据源
2. 提交：\`git add -A && git commit -m "feat(${TASK_NAME}): complete with completion report"\`
3. 告诉用户：\`"子任务 ${TASK_NAME} 完成，请回到主项目运行 merge-worktrees.sh 合并"\`

**不要自己合并回主分支，不要在 worktree 里 push。**

### 完成报告格式（WORKTREE-COMPLETION.md）

\`\`\`markdown
# Worktree 完成报告：${TASK_NAME}

- **任务名：** ${TASK_NAME}
- **完成时间：** YYYY-MM-DD HH:MM
- **分支：** feature/${TASK_NAME}

## 改动文件
| 文件路径 | 操作 | 对应 Commit |
|---------|------|------------|
| src/components/xxx.tsx | 新建 | abc1234 |
| src/services/xxx.ts | 修改 | def5678 |

## 验收证据
- typeCheck: ✅ / ❌（输出摘要）
- build: ✅ / ❌
- lint: ✅ / ❌
- test: ✅ / ❌（N/N 通过）

## 实现摘要
[2~3 句话描述做了什么]

## 遗留问题
[如有。没有写"无"]
\`\`\`

⚠️ 这个文件是主项目 Agent 恢复上下文和更新运行日志的唯一数据源。**必须写入，不可省略。**
CLAUDEEOF

echo "   ✅ CLAUDE.md（精简版，仅含单任务上下文）"

# ── 输出使用说明 ─────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ Worktree 已就绪: ${TASK_NAME}"
echo "═══════════════════════════════════════════"
echo ""
echo "下一步："
echo "  1. cd $WORKTREE_DIR"
echo "  2. 启动 Claude Code，发送：开始编码，任务 ${TASK_NAME}"
echo "     (Agent 读取 CLAUDE.md，直接进入 TDD 循环)"
echo ""
echo "⚠️  编码完成后，Agent 必须生成 WORKTREE-COMPLETION.md"
echo "    (这是主项目恢复上下文和更新运行日志的唯一数据源)"
echo ""
echo "合并回主项目："
echo "  cd $PROJECT_ROOT"
echo "  bash coding-assistant/scripts/merge-worktrees.sh ${TASK_NAME}"
