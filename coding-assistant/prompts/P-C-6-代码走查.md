# P-C-6 · 代码走查

## 触发阶段
SOP-08 走查

## 使用时机
自测通过后，claim 完成前。

---

## Prompt 模板

```
Use the verification-before-completion skill.

请对本轮改动进行完整走查：

改动文件：
[git diff --name-only main...HEAD 的输出]

## 1. 构建验证（fresh 跑，不接受"上次跑过"）

$ npm run typecheck
$ npm run build
$ npm run lint

请贴实际输出。任一失败先修复。

## 2. 测试验证（fresh 跑）

$ npm test

请贴实际输出。必须全绿。

## 3. 样式规范检查

- grep 检查无硬编码色值：# [0-9A-F] {6}
- grep 检查无裸 px 间距（排除 tokens.ts）
- 对照 docs/样式规范.md 逐条确认

## 4. 接口契约对照（分离架构）

逐字段核对代码实现与 coding-assistant/tools/03_接口契约/ 中的契约定义一致。

## 5. 改动范围检查

确认改动文件均在 CLAUDE.md「可读写」列表内，无越界。

## 6. 自欺欺人模式检查

逐文件检查：
- 函数体只有 TODO 或空 {} → ❌ 未实现
- 数据是硬编码 mock → ⚠️ 部分实现
- import 了但 JSX 中未使用 → ❌ 未接入
- onClick/回调是空函数 → ❌ 未实现
- setInterval 模拟替代真实逻辑 → ⚠️ 存根
- console.log 替代真实 API 调用 → ❌ 未实现

输出走查报告到 coding-assistant/tools/07_走查记录/走查报告-[日期].md，参照 coding-assistant/templates/走查报告模板.md
```

---

## 输出格式

走查报告含以下章节：
1. 构建验证结果（贴命令输出）
2. 测试验证结果（贴命令输出）
3. 样式规范检查（逐条结论 + 证据）
4. 接口契约对照表（字段级对比）
5. 改动范围检查（文件列表 + 边界对照）
6. 自欺欺人检查（逐项结论 + 代码位置）
7. 走查结论（通过 / 不通过）
