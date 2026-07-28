# Hooks 配置

> 将以下配置写入项目 `.claude/settings.json`（入仓，全员共享）。
> 不要放 `.claude/settings.local.json`（不入仓）。
>
> **Agent 在 SOP-01 项目起步阶段自动检测并安装。** 你不需要手动执行以下步骤。

---

## 依赖关系图

```
multi-agent-coding 多智能体协同
├── Claude Code ≥ 2.x                    ← 宿主平台
├── superpowers v5.0.7                   ← Skill 套件（writing-plans / executing-plans / TDD / verification / debugging）
├── oh-my-claudecode (OMC)               ← [可选] 专项 agent 路由
├── .claude/settings.json                ← 项目配置（入仓，全员共享）
│   ├── permissions.allow                ← 免确认命令（mvn / npm / git 等构建工具链）
│   ├── hooks.PreToolUse                 ← 文件边界守门（check-protected-files.mjs）
│   ├── hooks.PostToolUse                ← 低错拦截（lint + typecheck / compile）
│   └── hooks.Stop                       ← Claim 验证（test + build）
├── coding-assistant/scripts/check-protected-files.mjs    ← 文件边界保护脚本（模板自带）
├── coding-assistant/scripts/check-style.sh               ← [推荐] 样式硬编码检查
├── coding-assistant/scripts/init-worktree.sh             ← [worktree] 创建隔离 worktree
├── coding-assistant/scripts/merge-worktrees.sh           ← [worktree] 合并 worktree
├── coding-assistant/templates/worktree-CLAUDE.md         ← [worktree] 精简版 worktree CLAUDE.md 模板
└── coding-assistant/agents/                              ← Agent 定义（模板自带）
    ├── orchestrator.md                                   ← 含 worktree 判定 + 输出模式
    ├── reviewer.md
    └── module-dev.md
```

---

## settings.json 结构说明

> 模板自带的 `.claude/settings.json` 包含两部分：`permissions`（免确认命令）和 `hooks`（自动化触发）。

### permissions — 免确认命令

`permissions.allow` 列表中的命令在 Agent 调用 Bash 工具时**跳过权限确认弹窗**，减少打断。使用 glob 通配符匹配。

**模板默认配置：**

```json
{
  "permissions": {
    "allow": [
      "Bash(git --version)",
      "Bash(git rev-parse *)",
      "Bash(git status *)",
      "Bash(git log *)",
      "Bash(git diff *)",
      "Bash(git add *)",
      "Bash(git commit *)",
      "Bash(git merge *)",
      "Bash(git worktree *)",
      "Bash(git branch *)",
      "Bash(git -C *)",
      "Bash(claude --version)",
      "Bash(claude --list-skills *)",
      "Bash(claude --list-plugins *)",
      "Bash(claude plugins *)",
      "Bash(java -version)",
      "Bash(java --version)",
      "Bash(mvn *)",
      "Bash(mvnw *)",
      "Bash(node *)",
      "Bash(npm *)",
      "Bash(npx *)",
      "Bash(pnpm *)",
      "Bash(echo *)",
      "Bash(ls *)",
      "Bash(cat *)",
      "Bash(grep *)",
      "Bash(test *)",
      "Bash(find *)",
      "Bash(wc *)",
      "Bash(file *)",
      "Bash(head *)",
      "Bash(sort *)",
      "Bash(du *)",
      "Bash(printf *)",
      "Bash(mkdir *)"
    ]
  }
}
```

| 分类 | 命令 | 说明 |
|------|------|------|
| **构建工具** | `mvn *`, `mvnw *`, `npm *`, `npx *`, `pnpm *`, `node *` | 编译/测试/打包/脚本执行 |
| **版本检查** | `git --version`, `claude --version`, `java -version`, `java --version` | 环境检测使用的版本查询 |
| **Claude 管理** | `claude --version`, `claude --list-skills *`, `claude --list-plugins *`, `claude plugins *` | 版本 / Skill列表 / 插件列表 / 插件安装 |
| **Git 读操作** | `git rev-parse *`, `git status *`, `git log *`, `git diff *` | 仓库状态和历史查询 |
| **Git 写操作** | `git add *`, `git commit *`, `git merge *`, `git worktree *`, `git branch *` | 暂存、提交、worktree 并行、合并（SOP-06 场景三需要） |
| **文件诊断** | `echo *`, `ls *`, `cat *`, `grep *`, `test *`, `find *`, `wc *`, `file *`, `head *`, `sort *`, `du *`, `printf *` | 只读文件检查，SOP-01 环境检测必需 |
| **目录操作** | `mkdir *` | 创建目录（生成产出文件时可能需要） |

**安全边界：** 未放开的危险操作——`rm`/`rmdir`（删除）、`git push`（推送）、`git reset --hard`（强制回退）、`git branch -D`（删分支）、`chmod`/`chown`（权限变更）。这些操作仍需人工确认。

如需新增：

```json
"Bash(yarn *)"     // 如果用 yarn 替代 npm
"Bash(gradle *)"   // 如果用 Gradle 替代 Maven
"Bash(python *)"   // 如果用 Python 脚本
```

> 允许范围越宽，Agent 自主性越强，但风险也越大。按最小权限原则逐步放开。

### 完整 settings.json 结构

Agent 在 SOP-01 自动合并后，`.claude/settings.json` 最终结构：

```json
{
  "permissions": {
    "allow": [
      "Bash(git --version)",
      "Bash(git rev-parse *)",
      "Bash(git status *)",
      "Bash(git log *)",
      "Bash(git diff *)",
      "Bash(git add *)",
      "Bash(git commit *)",
      "Bash(git merge *)",
      "Bash(git worktree *)",
      "Bash(git branch *)",
      "Bash(git -C *)",
      "Bash(claude --version)",
      "Bash(claude --list-skills *)",
      "Bash(claude --list-plugins *)",
      "Bash(claude plugins *)",
      "Bash(java -version)",
      "Bash(java --version)",
      "Bash(mvn *)",
      "Bash(mvnw *)",
      "Bash(node *)",
      "Bash(npm *)",
      "Bash(npx *)",
      "Bash(pnpm *)",
      "Bash(echo *)",
      "Bash(ls *)",
      "Bash(cat *)",
      "Bash(grep *)",
      "Bash(test *)",
      "Bash(find *)",
      "Bash(wc *)",
      "Bash(file *)",
      "Bash(head *)",
      "Bash(sort *)",
      "Bash(du *)",
      "Bash(printf *)",
      "Bash(mkdir *)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{ "type": "command", "command": "node coding-assistant/scripts/check-protected-files.mjs", "timeout": 5 }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{ "type": "command", "command": "npm run lint -- --fix 2>/dev/null; npm run typecheck 2>/dev/null || true", "timeout": 30 }]
      }
    ],
    "Stop": [
      {
        "matcher": "*",
        "hooks": [{ "type": "command", "command": "npm test 2>/dev/null && npm run build 2>/dev/null || true", "timeout": 60 }]
      }
    ]
  }
}
```

---

## 一、安装检查清单

### 自动检查（Agent 执行）

Agent 在 SOP-01 项目起步阶段逐一检查以下项目。**可自动装的自动装，需人工的给出步骤。**

| 序号 | 组件 | 检查方式 | 安装方式 | 必须 |
|------|------|---------|---------|------|
| 1 | Claude Code | `claude --version` | 官网 claude.ai/code 安装 | ✅ |
| 2 | Node.js ≥ 18 | `node --version` | 官网 nodejs.org 安装 | ✅ |
| 3 | Git | `git --version` | 官网 git-scm.com 安装 | ✅ |
| 4 | superpowers | `ls ~/.claude/skills/writing-plans/SKILL.md` | `claude plugins install superpowers` | ✅ |
| 5 | hooks 配置 | `grep check-protected-files .claude/settings.json` | 自动合并写入 | ✅ |
| 6 | check-protected-files.mjs | `ls coding-assistant/scripts/check-protected-files.mjs` | 模板自带 | ✅ |
| 7 | check-style.sh | `ls coding-assistant/scripts/check-style.sh` | 从下方模板复制 | 推荐 |
| 8 | coding-assistant/agents/*.md | `ls coding-assistant/agents/orchestrator.md` 等 3 个 | 模板自带 | ✅ |
| 9 | 文件边界已填写 | `grep "\[模块名\]" coding-assistant/tools/00_项目配置/项目信息.md`（命中=未填）| Agent 扫描 src/ 自动填 | ✅ |
| 10 | OMC | `claude --list-plugins \| grep oh-my-claudecode` | **询问用户确认后安装** | 可选 |

---

## 二、Hook 配置（自动执行）

> Agent 在 SOP-01 根据 `coding-assistant/tools/00_项目配置/项目信息.md` 的「项目端型」选择对应命令。
> PreToolUse 的文件边界检查两类项目通用，PostToolUse 和 Stop 分端型。

### 前端项目 Hook 配置

```bash
node -e "
const fs = require('fs');
const hooks = {
  PreToolUse: [{
    matcher: 'Edit|Write',
    hooks: [{ type: 'command', command: 'node coding-assistant/scripts/check-protected-files.mjs', timeout: 5 }]
  }],
  PostToolUse: [{
    matcher: 'Edit|Write',
    hooks: [{ type: 'command', command: 'npm run lint -- --fix 2>/dev/null; npm run typecheck 2>/dev/null || true', timeout: 30 }]
  }],
  Stop: [{
    matcher: '*',
    hooks: [{ type: 'command', command: 'npm test 2>/dev/null && npm run build 2>/dev/null || true', timeout: 60 }]
  }]
};
let s = {};
try { s = JSON.parse(fs.readFileSync('.claude/settings.json','utf-8')); } catch {}
s.hooks = { ...(s.hooks||{}), ...hooks };
fs.writeFileSync('.claude/settings.json', JSON.stringify(s,null,2)+'\n');
console.log('✅ hooks merged (frontend)');
"
```

### 后端项目 Hook 配置

```bash
node -e "
const fs = require('fs');
const hooks = {
  PreToolUse: [{
    matcher: 'Edit|Write',
    hooks: [{ type: 'command', command: 'node coding-assistant/scripts/check-protected-files.mjs', timeout: 5 }]
  }],
  PostToolUse: [{
    matcher: 'Edit|Write',
    hooks: [{ type: 'command', command: 'mvn compile -q 2>/dev/null || true', timeout: 60 }]
  }],
  Stop: [{
    matcher: '*',
    hooks: [{ type: 'command', command: 'mvn test 2>/dev/null && mvn package -DskipTests 2>/dev/null || true', timeout: 120 }]
  }]
};
let s = {};
try { s = JSON.parse(fs.readFileSync('.claude/settings.json','utf-8')); } catch {}
s.hooks = { ...(s.hooks||{}), ...hooks };
fs.writeFileSync('.claude/settings.json', JSON.stringify(s,null,2)+'\n');
console.log('✅ hooks merged (backend)');
"
```

### 最终 `.claude/settings.json` 结构（前端示例）

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{ "type": "command", "command": "node coding-assistant/scripts/check-protected-files.mjs", "timeout": 5 }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{ "type": "command", "command": "npm run lint -- --fix 2>/dev/null; npm run typecheck 2>/dev/null || true", "timeout": 30 }]
      }
    ],
    "Stop": [
      {
        "matcher": "*",
        "hooks": [{ "type": "command", "command": "npm test 2>/dev/null && npm run build 2>/dev/null || true", "timeout": 60 }]
      }
    ]
  }
}
```

---

## 三、Hook 说明

| Hook | 时机 | 前端命令 | 后端命令 | 目的 |
|------|------|---------|---------|------|
| **PreToolUse** | Edit/Write 前 | `node coding-assistant/scripts/check-protected-files.mjs` | 同左 | 校验文件边界 |
| **PostToolUse** | Edit/Write 后 | `npm run lint; npm run typecheck` | `mvn compile -q` | 低错本地拦截 |
| **Stop** | Agent 声称完成时 | `npm test && npm run build` | `mvn test && mvn package -DskipTests` | 验证 claim |

> ★ **hook 失败会阻断 AI 操作**——AI 会看到错误信息并自动尝试修复。这就是"工具层兜底"的核心机制。

| Hook | 时机 | 目的 | 失败处理 |
|------|------|------|---------|
| **PreToolUse** | Edit/Write 前 | 校验目标文件是否在 CLAUDE.md 可读写列表内 | 拦截操作，报告越界文件 |
| **PostToolUse** | Edit/Write 后 | 自动跑 lint + typecheck | 低错本地拦截，不到 PR |
| **Stop** | Agent 声称完成时 | 跑全套 test + build | 验证 claim，失败阻止"已完成" |

> ★ **hook 失败会阻断 AI 操作**——AI 会看到错误信息并自动尝试修复。这就是"工具层兜底"的核心机制。

---

## 四、样式检查脚本

创建 `coding-assistant/scripts/check-style.sh`（chmod +x）：

```bash
#!/bin/bash
# Check for hardcoded colors and raw px values (exclude tokens.ts)
set -e

HARDCODED_COLORS=$(grep -rn '#[0-9A-Fa-f]\{6\}' src/ --include="*.tsx" --include="*.ts" --include="*.css" 2>/dev/null | grep -v tokens.ts || true)
RAW_PX=$(grep -rn ':\s*\d\+px' src/ --include="*.tsx" --include="*.css" 2>/dev/null | grep -v tokens.ts || true)

if [ -n "$HARDCODED_COLORS" ]; then
  echo "❌ Hardcoded colors found (must use import from @/tokens):"
  echo "$HARDCODED_COLORS"
  exit 1
fi

if [ -n "$RAW_PX" ]; then
  echo "⚠️  Raw px values found (consider using token values):"
  echo "$RAW_PX"
fi

echo "✅ Style check passed"
```
