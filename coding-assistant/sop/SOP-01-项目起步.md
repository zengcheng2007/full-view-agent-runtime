
# SOP-01 · 项目起步

## 硬门禁

> **SOP-01 是所有编码工作的硬门禁。环境检测未通过，禁止进入任何编码阶段。**
>
> Agent 在收到任何编码意图后，必须先完成本阶段全部检查项，自动安装可自动装的 skill 和插件，确认环境就绪后才能推进到后续阶段。不存在"先写代码再补环境"的选项。
>
> **技能安装优先于编码。** superpowers / hooks / agent 定义文件 / 文件边界保护脚本，四件缺一不可。

---

## 目标
分两阶段检查项目环境：基础环境（编码必需）+ 多智能体环境（协同必需）。
识别缺失项，Agent **自动安装可自动装的**，并告知人工安装步骤。

---

## 检查流程（不可跳过）

```
┌──────────────────────────────────────────────────────────────┐
│  Step 1: 基础环境检查 → 自动安装缺失项 → 报告                  │
│  Step 2: 多智能体检查 → 自动安装缺失项 → 报告                  │
│  Step 3: 门禁判断 → 全部就位？                                │
│    🔴 否 → 等待补充，Agent 停止推进                            │
│    🟢 是 → 继续                                               │
│  Step 4: 项目类型检测 → 全新 还是 已有？                       │
│    ├─ 全新项目 → 询问用户 → 填写项目信息（§三-A）              │
│    └─ 已有项目 → 分析工程 → 自动补全项目信息（§三-B）          │
│  Step 5: 文件边界填充 → 扫描 src/ 自动替换 [模块名]           │
│  Step 6: 场景检测 → 对照场景路由表 → 场景 A 继续判定子类型 → 裁剪流程          │
│  Step 7: 输出汇总报告 → 门禁 + 项目信息 + 场景 + 下一步        │
└──────────────────────────────────────────────────────────────┘
```

**Agent 必须严格执行此检查流程，不得以任何理由跳过。**

**检查命令规范：** 所有检查命令使用项目根目录相对路径（如 `grep xxx .claude/settings.json`），**禁止使用 `cd` 切换目录**。需跨项目检查时使用绝对路径直接传给命令（如 `git -C /path/to/project status`），避免 `cd && cmd` 组合触发 Claude Code 路径绕过保护。

### 安装优先级（全程适用）

一、二中各检查项缺失时，按以下优先级处理，**不询问用户要不要装**：

| 优先级 | 类别 | 缺失时行为 |
|--------|------|-----------|
| **P0 阻塞** | Node.js / Git / Claude Code | 无法自动装，告知用户手动安装链接，Agent 停止推进 |
| **P1 自动装** | superpowers / hooks / 文件边界 | Agent 自动执行安装命令，装完验证结果 |
| **P2 自动装** | check-style.sh | Agent 自动装，失败不阻塞流程 |
| **需用户确认** | OMC | 检测到缺失时**询问用户**是否安装，用户确认后才执行安装 |
| **P3 提示** | tokens.ts / 样式规范.md | Agent 提示缺失，不阻塞流程 |

---

## 一、基础环境检查（编码必需）

### 1.1 系统级依赖

| 检查项 | 检查命令 | 判据 |
|---|---|---|
| Node.js ≥ 18 | `node --version` | 版本号 ≥ v18.0.0 |
| Git | `git --version` | 正常输出版本 |
| Claude Code | `claude --version` | 正常输出版本 |

### 1.2 仓库与骨架

| 检查项 | 检查命令 | 判据 |
|---|---|---|
| Git 仓库 | `git rev-parse --git-dir` | 正常 |
| 依赖安装 | `ls node_modules/` 或 `npm ls --depth=0` | node_modules 存在 |
| 项目能起 | `npm run dev -- --version 2>/dev/null || true` | 无致命报错 |

### 1.3 CLAUDE.md

| 检查项 | 判据 |
|---|---|
| 文件存在 | `CLAUDE.md` 存在 |
| 含技术栈 | 匹配 `技术栈` 或 `techStack` |
| 含命名约定 | 匹配 `命名` 或 `PascalCase` 或 `camelCase` |
| 含样式铁律 | 匹配 `tokens` 或 `禁止硬编码` |
| 含 skill 声明 | 匹配 `writing-plans` 或 `superpowers` |
| 含验收命令 | 匹配 `tsc` 或 `typecheck` 或 `build` |

> 如 CLAUDE.md 不完整，参照 `CLAUDE.md` 当前文件补全缺失章节。

### 1.4 样式规范（前端必须）

| 检查项 | 检查命令 | 判据 |
|---|---|---|
| tokens.ts | `ls src/tokens.ts 2>/dev/null` | 文件存在 |
| 样式规范.md | `ls docs/样式规范.md 2>/dev/null` | 文件存在 |

---

## 二、multi-agent-coding 环境检查（协同必需）

### 2.1 superpowers Skill 套件

| Skill | 检查路径 | 判据 |
|---|---|---|
| writing-plans | `ls ~/.claude/skills/writing-plans/SKILL.md 2>/dev/null` | 文件存在即已安装 |
| executing-plans | `ls ~/.claude/skills/executing-plans/SKILL.md 2>/dev/null` | 文件存在即已安装 |
| test-driven-development | `ls ~/.claude/skills/test-driven-development/SKILL.md 2>/dev/null` | 文件存在即已安装 |
| verification-before-completion | `ls ~/.claude/skills/verification-before-completion/SKILL.md 2>/dev/null` | 文件存在即已安装 |
| systematic-debugging | `ls ~/.claude/skills/systematic-debugging/SKILL.md 2>/dev/null` | 文件存在即已安装 |

> **首次运行权限提示：** `ls ~/.claude/` 访问用户主目录会触发一次确认。选择 **"Yes, and don't ask again for reading from .claude"** 即可永久豁免，后续不再提示。

> **缺失时自动安装：**
> ```bash
> claude plugins install superpowers
> ```
> 如自动安装失败，告知用户手动安装：https://github.com/severin27/superpowers

### 2.2 Hooks 配置（PreToolUse 文件边界守门）

| 检查项 | 检查命令 | 判据 |
|---|---|---|
| `.claude/settings.json` 存在 | `ls .claude/settings.json` | 文件存在 |
| PreToolUse hook 已配 | `grep check-protected-files .claude/settings.json` | 命中 |
| PostToolUse hook 已配 | `grep typecheck .claude/settings.json` | 命中 |

> **缺失时自动安装：**
> 1. 确认 `coding-assistant/scripts/check-protected-files.mjs` 存在（模板自带）
> 2. 将 `coding-assistant/hooks/hooks-config.md` 中的 hook 配置写入 `.claude/settings.json`
>
> ```bash
> # 如果 settings.json 已存在，需合并而非覆盖
> node -e "
> const fs = require('fs');
> const hooks = {
>   PreToolUse: [{ matcher: 'Edit|Write', hooks: [{ type: 'command', command: 'node coding-assistant/scripts/check-protected-files.mjs', timeout: 5 }] }],
>   PostToolUse: [{ matcher: 'Edit|Write', hooks: [{ type: 'command', command: 'npm run lint -- --fix 2>/dev/null; npm run typecheck 2>/dev/null || true', timeout: 30 }] }],
>   Stop: [{ matcher: '*', hooks: [{ type: 'command', command: 'npm test 2>/dev/null && npm run build 2>/dev/null || true', timeout: 60 }] }]
> };
> let s = {};
> try { s = JSON.parse(fs.readFileSync('.claude/settings.json','utf-8')); } catch {}
> s.hooks = { ...(s.hooks||{}), ...hooks };
> fs.writeFileSync('.claude/settings.json', JSON.stringify(s,null,2));
> console.log('✅ hooks merged into .claude/settings.json');
> "
> ```

### 2.3 文件边界保护脚本

| 检查项 | 检查命令 | 判据 |
|---|---|---|
| `coding-assistant/scripts/check-protected-files.mjs` | `ls coding-assistant/scripts/check-protected-files.mjs` | 文件存在 |

> 本文件由模板自带（`coding-assistant/scripts/check-protected-files.mjs`），从模板复制即可。

### 2.4 样式检查脚本（推荐）

| 检查项 | 检查命令 | 判据 |
|---|---|---|
| `coding-assistant/scripts/check-style.sh` | `ls coding-assistant/scripts/check-style.sh` | 文件存在 |

> 缺失时从 `coding-assistant/hooks/hooks-config.md` 底部脚本模板复制创建。

### 2.5 Agent 定义文件

| Agent | 检查路径 | 判据 |
|---|---|---|
| orchestrator | `ls coding-assistant/agents/orchestrator.md` | 文件存在 |
| reviewer | `ls coding-assistant/agents/reviewer.md` | 文件存在 |
| module-dev | `ls coding-assistant/agents/module-dev.md` | 文件存在 |

> 这三个文件由模板自带，从模板复制项目时已就位。

### 2.6 文件边界配置

| 检查项 | 判据 |
|---|---|
| `coding-assistant/tools/00_项目配置/项目信息.md` 文件边界段已填写 | 可读写列表中无 `[模块名]` 占位符 |
| 可读写列表含实际目录 | 路径匹配 `src/` 下的真实目录 |
| 禁止修改列表含保护文件 | 至少含 `CLAUDE.md`、`coding-assistant/agents/*.md`、`src/tokens.ts` |

> 如含占位符 `[模块名]`，Agent 应扫描 `src/` 实际目录结构，**自动填建议值**，让用户确认。

### 2.7 OMC 集成（需用户确认）

| 检查项 | 检查命令 | 判据 |
|---|---|---|
| oh-my-claudecode | `claude --list-plugins \| grep oh-my-claudecode` | 命中 |

> **缺失时，Agent 必须询问用户是否安装**，不可自动跳过或自动安装：
>
> ```
> 检测到 OMC (oh-my-claudecode) 未安装。
> OMC 可增强多 Agent 协同能力（专项 agent 路由），但不是必需组件。
> 没有 OMC 时 orchestrator 会自动降级到内置 agent，流程不受影响。
>
> 是否安装 OMC？
>   [A] 安装 — 执行 claude plugins install oh-my-claudecode
>   [B] 跳过 — 使用内置 agent 降级方案
> ```
>
> 用户选择 [A] 后 Agent 执行安装命令并报告结果。
> 用户选择 [B] 后记录并继续推进。

---

## 三、项目初始化（门禁通过后执行）

门禁 🟢 放行后，Agent 在执行场景检测前，必须先完成项目信息初始化。根据项目状态分两条路径：

### 项目类型检测

Agent 通过以下信号自动判断：

| 信号 | 倾向 |
|------|------|
| `src/` 目录存在且有代码文件 | **已有项目** |
| `package.json` / `pom.xml` / `build.gradle` 存在 | **已有项目** |
| `CLAUDE.md` 含完整技术栈和命名约定 | **已有项目** |
| `src/` 为空或不存在，无 package.json | **全新项目** |
| 用户说"从零""新建""create" | **全新项目** |
| 仅存在本模板文件，无其他业务代码 | **全新项目** |

**判断不确定时，直接问用户：**"检测到项目当前状态不明确，请问这是一个全新项目还是已有代码的项目？"

---

### 路径 A：全新项目（交互式询问）

Agent 按以下清单逐项询问用户，**不得猜测或跳过**。每项提供默认建议值。

#### A.1 必问项（阻塞推进）

| 询问项 | 目的 | 默认建议 |
|--------|------|---------|
| 项目名称 | 填入项目信息 | 当前目录名 |
| 项目简介（一句话）| 理解业务上下文 | — |
| 前端技术栈 | 决定组件库/样式/测试策略 | React + TypeScript + Tailwind |
| 是否有后端 | 决定架构形态 | 一体化（无独立后端） |
| 后端技术栈（如有）| 决定 API 生成策略 | FastAPI / Spring Boot |
| UI 组件库 | 决定组件生成规则 | Ant Design / 自研 |
| 状态管理 | 决定 store 生成策略 | Zustand / Vuex |
| 测试框架 | 决定测试生成策略 | Vitest / Jest |

#### A.2 推导项（Agent 自动填）

根据用户回答自动推导：

| 推导项 | 推导规则 |
|--------|---------|
| 架构形态 | 无后端 → 一体化；有后端 → 分离 |
| 样式方案 | Tailwind / CSS Modules / styled-components（匹配 UI 库） |
| 包管理器 | 检测 `pnpm-lock.yaml` / `package-lock.json` / `yarn.lock` |
| 验收命令 | 根据技术栈生成默认命令 |
| 关键约束 | 禁用 any / tokens 导入 / 命名约定 / conventional commits |

#### A.3 询问格式

Agent 以结构化表单形式一次性列出所有必问项，让用户批量填写：

```
请填写以下项目信息（直接回复"项目名 + 技术栈"即可，其余项我会自动推导）：

1. 项目名称：
2. 一句话描述：
3. 前端技术栈：[默认：React + TypeScript + Tailwind]
4. 有无后端？[有/无]
5. 后端技术栈（如有）：
6. UI 组件库：[默认：Ant Design]
```

用户回复后，Agent 将信息写入 `coding-assistant/tools/00_项目配置/项目信息.md`，推导项自动补全。

---

### 路径 B：已有项目（自动分析工程）

Agent 分析整个项目工程，**自动补全** `coding-assistant/tools/00_项目配置/项目信息.md`。分析完成后向用户汇报，让用户确认或修正。

#### B.1 项目端型判定

Agent 先判定项目端型，再按对应清单探测：

| 信号 | 判定 |
|------|------|
| 根目录有 `package.json`，无 `pom.xml` / `build.gradle` | **前端** |
| 根目录有 `pom.xml`（含 `<parent>` 或 `<modules>`）或有 `build.gradle` | **后端** |
| 根目录同时存在 `package.json` + `pom.xml` | **全栈**（两端分别探测） |
| 仅有本模板文件，无其他代码 | **全新项目** → 走路径 A |

#### B.2 探测清单 — 前端项目

| 探测目标 | 探测方法 | 写入字段 |
|----------|---------|---------|
| 项目名称 | 读 `package.json` 的 `name` 或目录名 | 项目名称 |
| 技术栈-前端 | 读 `package.json` dependencies：react/vue/angular + typescript + tailwind/antd/element，**对照 `coding-assistant/tools/00_项目配置/前端工程基线.md` 识别工程模式** | 技术栈-前端 |
| 构建工具 | 读配置文件：vue.config.js / vite.config.ts / craco / webpack；lock 文件判定包管理器 | 构建工具 |
| 架构形态 | 有后端目录 → 分离；仅前端 → 一体化 | 架构形态 |
| 状态管理 | 读 `package.json`：zustand/vuex/redux/pinia | 状态管理 |
| UI 组件库 | 读 `package.json`：antd/element-plus/@mui/vant 等 | UI 组件库 |
| 样式方案 | 读 `package.json`：sass/less/tailwindcss + 配置文件 | 样式方案 |
| 类型系统 | 读 `tsconfig.json` 或扫描 `.ts/.tsx` 文件比例 | 技术栈-前端 |
| 测试框架 | 读 `package.json` devDependencies：vitest/jest/playwright | 测试框架 |
| 源码目录 | 扫描顶层目录：src/ / app/ 等 | 目录结构 |
| 目录分层 | 扫描 `src/` 下 views/pages/components/router/store/utils/mock/api，**对照前端工程基线确认分层完整性** | 目录结构 |
| 路由模式 | 读 `router/index.*`：hash / history | 特殊说明 |
| API 层结构 | 扫描 `src/api/`，识别 request.* 基础设施 + 业务模块 | 文件边界 |
| Mock 方案 | 扫描 `src/mock/` 或读 `package.json`：msw/axios-mock-adapter | 特殊说明 |
| 移动端/H5 | 读 `package.json`：postcss-pxtorem / flexible | 特殊说明 |
| 命名约定 | 读 3~5 个组件文件：PascalCase? kebab-case? | 关键约束 |
| tokens 位置 | 搜索 `tokens.ts` 或 `theme.ts` 或 CSS 变量文件 | 可只读列表 |
| 验收命令 | 读 `package.json` scripts：serve/dev/build/lint/test/typecheck | 验收命令 |
| 仓库地址 | `git remote get-url origin` | 项目仓库地址 |

#### B.3 探测清单 — 后端项目

| 探测目标 | 探测方法 | 写入字段 |
|----------|---------|---------|
| 项目名称 | 读 `pom.xml` 的 `<artifactId>` 或目录名 | 项目名称 |
| 技术栈-后端 | 读 `pom.xml`：spring-boot-starter / mybatis / postgresql / redis 等依赖，**对照 `coding-assistant/tools/00_项目配置/后端工程基线.md` 标注版本差距** | 技术栈-后端 |
| 构建工具 | 读 `pom.xml` 或 `build.gradle`：Maven / Gradle | 构建工具 |
| 架构形态 | 读 `pom.xml` `<modules>` → 微服务多模块；无 `<modules>` → 单体 | 架构形态 |
| JDK 版本 | 读 `pom.xml` `<java.version>` 或 `maven.compiler.source`，**对照后端工程基线判定差距** | 技术栈-后端 |
| 组件版本差距 | 逐项对比推荐基线：Java/Spring Boot/Cloud/MyBatis-Plus/ES/API文档/熔断/追踪/认证/JSON/Hutool | 需确认项 |
| 测试框架 | 读 `pom.xml`：junit / testng / mockito | 测试框架 |
| 模块列表 | 读父 POM 的 `<modules>` 或扫描子目录含 `pom.xml` 的目录 | 目录结构 |
| 包结构 | 扫描 `src/main/java/` 下的 controller/service/mapper/entity/config/utils | 目录结构 |
| 配置位置 | 扫描 `src/main/resources/`：bootstrap.yml / application.yml / mapper/ | 可只读列表 |
| 命名约定 | 读 3~5 个类文件：PascalCase? camelCase? 包名规范? | 关键约束 |
| MyBatis XML | 扫描 `src/main/resources/mapper/` 子目录（mysql/pgsql 等）| 文件边界 |
| 本地 JAR | 扫描 `lib/` 目录 | 可只读列表 |
| CLAUDE.md | 读取现有 CLAUDE.md，提取已有约定 | 多个字段 |
| 验收命令 | `mvn compile` / `mvn test` / `mvn checkstyle:check` | 验收命令 |
| 仓库地址 | `git remote get-url origin` | 项目仓库地址 |

#### B.4 自动分析执行步骤

**前端项目：**
1. 读 `package.json`，提取技术栈、依赖、scripts
2. 读 `CLAUDE.md`（如存在），提取已有约定和规则
3. 扫描 `src/` 目录结构，识别模块划分和命名模式
4. 搜索 tokens/theme 文件路径
5. 将分析结果填入 `coding-assistant/tools/00_项目配置/项目信息.md`

**后端项目：**
1. 读父 `pom.xml`，提取 `<modules>`、`<java.version>`、Spring Boot / MyBatis 等依赖
2. 扫描各模块 `src/main/java/`，识别 controller/service/mapper/entity/config 分层
3. 扫描 `src/main/resources/mapper/`，识别 MyBatis XML 目录结构
4. 读 `application.yml` / `bootstrap.yml`，提取 Nacos/DB/Redis 连接信息（仅确认存在，不读内容）
5. 读 3~5 个 Controller/Service/Mapper 类，确认命名约定和包结构
6. 扫描 `lib/` 目录，识别本地 JAR 依赖
7. 读 `CLAUDE.md`（如存在），提取已有约定和规则
8. 将分析结果填入 `coding-assistant/tools/00_项目配置/项目信息.md`
9. 将分析结果和证据向用户汇报（见下方输出格式）

#### B.5 汇报格式

**前端项目示例：**

```
## 项目分析结果
  项目端型：前端
  项目名称：xxx-admin
  架构形态：一体化

### 已自动补全
| 字段 | 值 | 证据 |
|------|-----|------|
| 技术栈-前端 | React 18 + TypeScript + Tailwind | package.json |
| 构建工具 | pnpm | pnpm-lock.yaml |
| 状态管理 | Zustand | package.json + src/stores/ |
| UI 组件库 | Ant Design 5.x | package.json |
| 测试框架 | Vitest | package.json devDependencies |
| 验收命令 | pnpm exec tsc --noEmit | package.json scripts |
| 文件边界 [模块名] | dashboard, common, auth | src/components/ 子目录 |

### 需确认
| # | 问题 | Agent 推测 | 请确认 |
|---|------|-----------|--------|
| 1 | tokens 文件路径 | 未找到 tokens.ts | 请指定路径或确认无 tokens |
| 2 | 命名约定 | 观测到 PascalCase 组件 + camelCase 函数 | 确认 |
```

**后端项目示例：**

```
## 项目分析结果
  项目端型：后端
  项目名称：xxx-service
  架构形态：微服务多模块（3 个业务模块 + boot-utils）

### 已自动补全
| 字段 | 值 | 证据 |
|------|-----|------|
| 技术栈-后端 | Spring Boot 2.7 + MyBatis Plus + PostgreSQL + Redis | pom.xml 依赖 |
| 构建工具 | Maven 3.8 | pom.xml + mvnw |
| JDK 版本 | 17 | pom.xml <java.version> |
| 测试框架 | JUnit 5 + Mockito | pom.xml 依赖 |
| 模块列表 | xxx-core, xxx-api, xxx-admin | 父 POM <modules> |
| 包结构 | cn.com.company.xxx.controller/service/mapper/entity/config | src/main/java/ 扫描 |
| 验收命令 | mvn compile / mvn test | pom.xml build |
| MyBatis XML | src/main/resources/mapper/mysql/ | 扫描确认 |
| 本地 JAR | lib/xxx-sdk-1.2.jar | lib/ 目录 |

### 需确认
| # | 问题 | Agent 推测 | 请确认 |
|---|------|-----------|--------|
| 1 | 是否所有模块 Agent 都可写 | 仅分析到目录结构 | 请指定可读写模块范围 |
| 2 | 命名约定 | 观测到 PascalCase 类 + camelCase 方法 | 确认 |
| 3 | 是否有 Nacos 配置中心 | application.yml 含 Nacos 配置 | 确认 |
```

> **原则：Agent 分析后汇报，人确认，不猜测不自动决策。** 分析有不确定的标注为【需确认】。

---

## 四、场景检测

项目信息就位后，**立即**检查 `coding-assistant/tools/01_需求输入/` 目录。

### 4.0 输入材料就绪检查（硬逻辑）

**Agent 必须在场景检测前执行此步骤，不可跳过。**

1. 列出 `coding-assistant/tools/01_需求输入/` 下所有文件
2. 排除 `放入说明.md`（模板占位文件，不算实际输入）
3. 判断是否有实际输入材料：

```
有实际输入文件 → 继续 §4.1 场景检测
无实际输入文件 → 进入 §4.0-A/B 交互分支
```

#### 4.0-A 无输入材料时的交互分支

Agent 检测到 `coding-assistant/tools/01_需求输入/` 为空（仅含 `放入说明.md`）时，**不可直接终止流程**。使用 `AskUserQuestion` 工具向用户呈现选择：

```
问题: 如何提供需求输入？
  [A] 我自行放入文件 — 将需求文档、数据样例、设计稿等放入 coding-assistant/tools/01_需求输入/ 目录
  [B] 对话生成 — Agent 逐项询问需求，帮我生成输入文件
```

#### 4.0-A-1 用户选择 [A] 我自行放入文件

Agent 行为：
1. 告知用户：`请将需求材料放入 coding-assistant/tools/01_需求输入/ 目录，放好后回复任意内容。`
2. 更新 `coding-assistant/tools/99_运行日志/当前状态.md`：`当前阻塞项: 等待用户放入需求输入材料`
3. 设置 `阶段状态 = 等待人工`
4. **停止推进，等待用户回复**
5. 用户回复后，使用 `AskUserQuestion` 工具再次呈现选择：

```
问题: 需求材料是否已放入？
  [A] 文件已放入，继续 — Agent 重新检查目录并推进场景检测
  [B] 改为对话生成 — Agent 逐项询问需求，帮我生成输入文件
```

6. 用户选择 [A] → 重新列出 `coding-assistant/tools/01_需求输入/` 下文件 → 确认文件到位后继续 §4.1 场景检测
7. 用户选择 [B] → 进入 §4.0-B 对话生成流程

#### 4.0-B 对话生成输入文件

Agent 行为：

**Step 1：读取项目配置获取上下文**

读 `coding-assistant/tools/00_项目配置/项目信息.md`，获取项目端型（前端/后端/全栈），用于针对性提问。

**Step 2：结构化询问（逐项，不跳过）**

按以下顺序询问用户，**每次只问一项**，等用户回复后再问下一项：

| 序号 | 询问内容 | 目的 | 生成文件 |
|------|---------|------|---------|
| 1 | "请用 2-3 句话描述你想做的功能/项目是什么？" | 核心目标 | 需求清单 |
| 2 | "涉及哪些用户角色？每个角色做什么操作？" | 用户场景 | 需求清单 |
| 3 | "有没有参考数据或字段格式？没有就说'无'。有的话举个例子即可。" | 数据口径 | 数据样例 |
| 4 | "有原型图、设计稿或架构图吗？有的话描述或截图，没有说'无'。" | 设计输入 | 需求清单 |

**Step 3：根据用户回答推断场景**

用户描述中检测关键词（大屏/一条龙/从零/前端/后端/设计稿等）→ 对照 `coding-assistant/sop/场景路由表.md` 判定场景 A~E。场景 A 继续判定子类型（全栈/前端/后端），优先自动检测，不确定时 `AskUserQuestion`。

如无法确定，让用户确认：
```
根据你的描述，我推测场景可能是 [X] 或 [Y]。
- 场景 X：[简述]
- 场景 Y：[简述]
请确认哪个更接近你的预期？
```

**Step 4：生成需求输入文件**

根据用户回答和判定的场景，生成以下文件：

| 文件 | 内容 | 模板参考 |
|------|------|---------|
| `coding-assistant/tools/01_需求输入/需求清单.md` | 项目目标 + 用户角色 + 场景描述 + 核心功能点 | 结构化 Markdown |
| `coding-assistant/tools/01_需求输入/数据样例.md` | 字段名 + 类型 + 示例值（用户提供了才生成） | 表格格式 |
| `coding-assistant/tools/01_需求输入/设计说明.md` | 原型/设计稿信息（用户提供描述或截图引用） | 文字描述 |

**Step 5：让用户确认生成的文件**

```
以下是根据你的描述生成的输入文件：

coding-assistant/tools/01_需求输入/需求清单.md
- 项目目标：[一句话]
- 用户角色：[列表]
- 场景：[A~E]
- 核心功能：[列表]

coding-assistant/tools/01_需求输入/数据样例.md [如有]
- 字段：[列表]

确认无误后回复"确认"，或指出需要修改的部分。我会继续场景检测和流程推进。
```

**Step 6：确认后继续**

用户确认后 → 继续 §4.1 场景检测（此时输入文件已就位）。

### 4.1 检测步骤

1. 列出 `coding-assistant/tools/01_需求输入/` 下所有文件，标注类型：
   - `.md` / `.docx` → 需求文档
   - `.png` / `.jpg` / Figma 链接 → 原型/设计稿
   - `.xlsx` / `.csv` / `.json` → 数据文件
   - `.yaml` / Swagger 链接 → API 契约

2. 检查用户触发语中的关键词（"大屏""一条龙""Figma""从零"等）

3. 对照 `coding-assistant/sop/场景路由表.md` §场景检测规则 判定场景 A~E

4. 如无法确定，列出判断依据让用户确认

### 4.2 裁剪后流程输出

判定场景后，输出该场景的阶段清单：

```
🔍 场景判定：[场景X] · [场景名称]

输入特征：
  - [列出实际检测到的文件/触发词]

裁剪后流程（[N] 个阶段）：
  ✅ SOP-01 项目起步（当前）
  ✅ SOP-02 [阶段名]
  ⏭️ SOP-03 [阶段名] — 跳过（原因）
  ✅ SOP-04 [阶段名]
  ...

预计执行 [N] 个阶段，跳过 [10-N] 个。

确认后进入 [第一个待执行阶段名称]。
```

---

## 五、输出格式

向用户汇报完整的装机检查结果。**必须包含门禁判断。**

```
═══════════════════════════════════════════
  项目起步 · 环境检测 + 场景路由
═══════════════════════════════════════════

## 一、基础环境
✅ Node.js v20.11.0
✅ Git 2.43.0
✅ Claude Code 2.x
✅ 仓库 + node_modules
✅ CLAUDE.md
⚠️ tokens.ts 缺失（P3 提示，不阻塞）

## 二、Skill 与插件（已自动安装）
✅ superpowers v5.0.7 — 已安装（5/5 skills）
✅ hooks — 已合并到 .claude/settings.json
✅ check-protected-files.mjs — 就位
✅ Agent 定义（orchestrator / reviewer / module-dev）— 就位
⚠️ OMC — 未安装（已询问用户 → 用户选择 [跳过]）

## 三、自动安装结果
  已安装：superpowers / hooks（2 项）
  已跳过：无
  需用户确认：OMC（用户选择跳过）

## 四、项目初始化
  项目类型：[全新项目 / 已有项目]
  [全新项目] 已询问并填写：项目名称 / 技术栈 / 架构形态 / ...
  [已有项目] 已自动分析并补全：12/16 项，2 项需确认

## 五、场景判定
🔍 检测到 7 个 Excel 文件 + 1 个 PNG 原型，用户提到"大屏"
   → 场景 D：原型+数据重构页面

## 裁剪后流程（5 个阶段）
  ✅ SOP-01 项目起步（当前）
  ⏭️ SOP-02 需求吃透 — 跳过（原型图即需求）
  ⏭️ SOP-03 技术方案 — 跳过（纯前端页面）
  ⏭️ SOP-04 接口对齐 — 替换为：Excel 数据处理（pandas）
  ✅ SOP-05 任务拆分
  ✅ SOP-06 编码（HTML+Chart.js）
  ✅ SOP-07 自测
  ✅ SOP-08 走查
  ⏭️ SOP-09 联调 — 跳过（无后端）
  📌 SOP-10 沉淀 — 按需

## ══ 门禁判断 ══
P0 阻塞项：0   ✅
P1 自动装项：全部就位   ✅
→ 🟢 环境就绪，可以进入编码流程

## 下一步
确认后进入"原型+数据理解"（场景 D 专属阶段）。
```

### 门禁判断规则

| 门禁结果 | 条件 | Agent 行为 |
|----------|------|-----------|
| 🟢 放行 | P0 全部通过 + P1 全部就位 | 进入场景的第一个阶段 |
| 🟡 条件放行 | P0/P1 通过，P2/P3 有缺失 | 进入编码但提示注意事项 |
| 🔴 阻断 | P0 或 P1 有未解决项 | 停止推进，等待用户补充 |

---

## 输出

> Agent 完成本阶段后，必须将产出写入以下路径。

| # | 写入路径 | 内容 | 下游阶段 |
|---|---------|------|---------|
| 1 | `coding-assistant/tools/00_项目配置/项目信息.md` | 项目基本信息、技术栈、文件边界、验收命令 | 全部 |
| 2 | `coding-assistant/tools/99_运行日志/当前状态.md` | 装机结果、项目初始化结果、场景判定、门禁判断 | 全部 |
| 3 | `coding-assistant/tools/99_运行日志/产出索引.md` | 更新 SOP-01 行（状态+产出文件） | — |
| 4 | `coding-assistant/tools/99_运行日志/执行记录.md` | 追加启动动作记录 | — |

