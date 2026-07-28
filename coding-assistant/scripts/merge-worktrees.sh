#!/bin/bash
# merge-worktrees.sh — 合并已完成的 worktree 并清理
#
# 用法: bash coding-assistant/scripts/merge-worktrees.sh <task-name> [task-name2 ...]
#
# 示例: bash coding-assistant/scripts/merge-worktrees.sh user-list
#       bash coding-assistant/scripts/merge-worktrees.sh user-list order-form payment-flow
#
# 执行后:
#   - git merge --no-ff feature/<task>（合并到当前分支）
#   - git worktree remove <项目>-<task>
#   - git branch -d feature/<task>
#
# 如果有冲突 → 报冲突文件列表，等待人手动解决。不自动 --force。

set -e

PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [ -z "$PROJECT_ROOT" ]; then
    echo "❌ 不在 git 仓库中。请在主项目目录运行此脚本。"
    exit 1
fi

# 确保编码产物目录存在
CODING_OUTPUT_DIR="$PROJECT_ROOT/coding-assistant/tools/05_编码产物"
mkdir -p "$CODING_OUTPUT_DIR"

TASK_NAMES=("$@")
if [ ${#TASK_NAMES[@]} -eq 0 ]; then
    echo "用法: bash coding-assistant/scripts/merge-worktrees.sh <task-name> [task-name2 ...]"
    echo ""
    echo "活跃的 worktree:"
    git worktree list
    exit 1
fi

MERGED=0
FAILED=0
SKIPPED=0

for TASK in "${TASK_NAMES[@]}"; do
    BRANCH="feature/${TASK}"
    WORKTREE_DIR="${PROJECT_ROOT}-${TASK}"

    echo ""
    echo "── 合并 ${TASK} ──"

    # 检查分支是否存在
    if ! git branch --list "$BRANCH" | grep -q "$BRANCH"; then
        echo "   ⏭️  分支 $BRANCH 不存在，跳过"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # 检查 worktree 是否还存在
    if [ -d "$WORKTREE_DIR" ]; then
        # 检查是否有未提交的改动
        UNCOMMITTED=$(git -C "$WORKTREE_DIR" status --short 2>/dev/null || echo "")
        if [ -n "$UNCOMMITTED" ]; then
            echo "   ⚠️  ${TASK}: worktree 有未提交的改动"
            echo "$UNCOMMITTED"
            echo "   请先在 worktree 中提交或 stash，再合并。"
            FAILED=$((FAILED + 1))
            continue
        fi
    fi

    # 合并
    echo "   git merge --no-ff $BRANCH"
    if git merge --no-ff "$BRANCH" -m "merge: ${TASK} from worktree" 2>&1; then
        echo "   ✅ ${TASK} 合并成功"

        # 保留完成报告（worktree 删除后将无法访问）
        COMPLETION_FILE="$WORKTREE_DIR/WORKTREE-COMPLETION.md"
        if [ -f "$COMPLETION_FILE" ]; then
            cp "$COMPLETION_FILE" "$CODING_OUTPUT_DIR/WORKTREE-COMPLETION-${TASK}.md"
            echo "   📋 完成报告已保留: coding-assistant/tools/05_编码产物/WORKTREE-COMPLETION-${TASK}.md"
        else
            echo "   ⚠️  未找到 WORKTREE-COMPLETION.md，将无法自动恢复上下文"
        fi

        # 删除 worktree
        if [ -d "$WORKTREE_DIR" ]; then
            git worktree remove "$WORKTREE_DIR" 2>/dev/null || {
                echo "   ⚠️  worktree remove 失败，手动清理: rm -rf $WORKTREE_DIR && git worktree prune"
            }
        fi

        # 删除分支
        git branch -d "$BRANCH" 2>/dev/null || {
            echo "   ⚠️  分支 $BRANCH 删除失败（可能已被合并删除）"
        }

        MERGED=$((MERGED + 1))
    else
        echo "   ❌ ${TASK} 合并冲突！请手动解决："
        echo "      git status                    # 查看冲突文件"
        echo "      # 解决冲突后:"
        echo "      git add <冲突文件>"
        echo "      git commit -m 'merge: ${TASK} (conflict resolved)'"
        echo "      git worktree remove $WORKTREE_DIR"
        echo "      git branch -d $BRANCH"
        FAILED=$((FAILED + 1))
        # 不继续合并后续任务，避免累积冲突
        break
    fi
done

echo ""
echo "═══════════════════════════════════════════"
echo "  合并完成: ✅ ${MERGED} | ❌ ${FAILED} | ⏭️ ${SKIPPED}"
echo "═══════════════════════════════════════════"

if [ $FAILED -gt 0 ]; then
    echo ""
    echo "⚠️  有合并失败的任务，请手动处理后再继续。"
    echo ""
    echo "手动合并步骤："
    echo "  1. git status                     # 查看冲突文件"
    echo "  2. # 编辑冲突文件，解决冲突"
    echo "  3. git add <冲突文件>"
    echo "  4. git commit"
    echo "  5. 删除 worktree: git worktree remove <路径>"
    echo "  6. 删除分支: git branch -d feature/<task>"
fi

if [ $MERGED -gt 0 ] && [ $FAILED -eq 0 ]; then
    # 提交保留的完成报告
    COMPLETION_FILES=$(ls "$CODING_OUTPUT_DIR"/WORKTREE-COMPLETION-*.md 2>/dev/null || echo "")
    if [ -n "$COMPLETION_FILES" ]; then
        echo ""
        echo "提交 Worktree 完成报告..."
        git add "$CODING_OUTPUT_DIR"/WORKTREE-COMPLETION-*.md 2>/dev/null || true
        if git diff --cached --quiet; then
            echo "   (完成报告已在合并中提交，跳过)"
        else
            git commit -m "chore: archive worktree completion reports" 2>/dev/null || true
            echo "   ✅ 完成报告已提交"
        fi
    fi

    echo ""
    echo "所有 worktree 已合并。回到主项目编码流程："
    echo "  cd $PROJECT_ROOT"
    echo "  claude"
    echo "  发送：继续编码（Agent 会读取完成报告自动更新运行日志并进入 SOP-07）"
fi
