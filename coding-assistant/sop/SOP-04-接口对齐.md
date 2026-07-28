# SOP-04 · 接口对齐

## 目标
基于技术方案的接口设计草案，产出可执行的接口契约文档。

---

## 前置条件
- SOP-03 技术方案人工审核通过

---

## 输入

> Agent 执行本阶段前，必须先验证以下文件是否存在。
> **输入缺失时，不可直接终止流程。**

| # | 读取路径 | 用途 | 适用场景 | 来源阶段 |
|---|---------|------|---------|---------|
| 1 | `coding-assistant/tools/02_技术方案/技术方案-[日期]-[feature].md` | 接口设计草案（§3） | A, E | SOP-03 |

**输入缺失处理：** 技术方案文件不存在时，Agent 使用 `AskUserQuestion`：

```
问题: 技术方案未就绪，无法直接生成接口契约，如何处理？
  [A] 回到 SOP-03 技术方案 — 完成技术方案后带着接口草案再来
  [B] 对话引导 — 我现在描述接口需求（字段/类型/错误码），Agent 直接生成契约（标记为"对话生成"）
```

用户选择 [B] 时，Agent 通过对话引导用户描述接口（方法/路径/请求体/响应体/错误码），生成 `api.md` 或 OpenAPI yaml，标注"对话生成，待确认"。

> **场景差异**：A-前端 读取已有 API 文档（非技术方案），C 直接基于 OpenAPI/Swagger 文件生成，D 跳过本阶段（替换为 Excel→JSON 数据处理）。详见 `coding-assistant/sop/场景路由表.md`。

---

## 架构形态对应的产出

| 架构形态 | 产出 | 落地要求 |
|---|---|---|
| 一体化（一人前后端）| `api.md` 简明列表 | 入仓即可 |
| 分离（前后端各自调 AI）| OpenAPI 完整契约 | **契约 PR 必须先合，再写代码** |

> ★ **分离架构下契约 PR 必须先合，再写代码**——契约不稳前面写多少都是返工。

---

## 执行步骤

1. 读取 `coding-assistant/tools/02_技术方案/` 中的接口设计草案
2. 套用 `coding-assistant/prompts/P-C-3-接口契约生成.md` 生成契约
3. 一体化：输出 `coding-assistant/tools/03_接口契约/api.md`
4. 分离架构：输出 `coding-assistant/tools/03_接口契约/openapi.yaml`
5. **【硬门禁】文件校验**（见下方 §校验流程，必须执行）
6. 校验通过后，分离架构下提醒用户：**契约 PR 必须先合再写代码**

---

## 校验流程（硬门禁）

> **Agent 在生成契约文件后，必须立即执行校验。校验不通过 → 修复 → 重新校验，直到通过。**

### OpenAPI YAML 校验

**Step 1：YAML 语法解析校验**

```bash
python -c "import yaml; yaml.safe_load(open('coding-assistant/tools/03_接口契约/openapi.yaml')); print('YAML syntax OK')" 2>&1
```

> 如 PyYAML 未安装，降级使用 Step 2 文本模式检查。

**Step 2：文本模式常见缺陷检查**

```bash
node -e "
const fs=require('fs');
const content=fs.readFileSync('coding-assistant/tools/03_接口契约/openapi.yaml','utf-8');
const lines=content.split('\n');
let issues=[];
lines.forEach((line,i)=>{
  const ln=i+1;
  // 1. 双引号字符串内未转义的双引号（最常见错误）
  const m=line.match(/example:\s*\"[^\"]*\"[^\"]*\"/);
  if(m) issues.push('Line '+ln+': example contains unescaped quotes — use YAML native syntax instead of JSON inside strings');
  // 2. JSON 对象/数组写在 YAML 值里
  if(/\{\s*type:/.test(line)) issues.push('Line '+ln+': JSON object embedded in YAML — use YAML mapping syntax');
  if(/\[\s*\"[^\"]+\"/.test(line)) issues.push('Line '+ln+': JSON array embedded in YAML — use YAML list syntax (- item)');
  // 3. 缩进不一致（tab vs space）
  if(line.match(/^\t+/)) issues.push('Line '+ln+': tab indentation detected — use spaces only');
});
if(issues.length>0){
  console.log(issues.length+' issue(s) found:');
  issues.forEach(i=>console.log('  - '+i));
  process.exit(1);
} else {
  console.log('Text pattern check passed');
}
"
```

**Step 3：OpenAPI 规范结构校验**

```bash
grep -E "^openapi:|^info:|^paths:|^components:" coding-assistant/tools/03_接口契约/openapi.yaml | wc -l | xargs -I{} sh -c 'test {} -ge 3 && echo "Structure OK: {} sections" || (echo "MISSING_SECTIONS: only {} of 4 required sections (openapi/info/paths/components)"; exit 1)'
```

### api.md 校验

```bash
grep -c "| 方法 | 路径 |" coding-assistant/tools/03_接口契约/api.md && \
grep -c "| 字段 | 类型 |" coding-assistant/tools/03_接口契约/api.md && \
echo "api.md structure check passed"
```

### 校验失败处理

Agent 行为：
1. 记录校验失败的详细信息（哪个检查失败了、具体行号、原因）
2. **自动修复**：分析错误原因，直接修改文件，重新校验
3. 同一类错误修复 3 次仍未通过 → 停止，告知用户问题，请求人工审查
4. 校验通过后，将校验结果写入 `coding-assistant/tools/99_运行日志/当前状态.md`

---

## 契约质量要求

- 所有请求/响应字段含类型和示例
- 所有错误码列出（业务码 + HTTP 状态）
- 字段命名统一：接口字段 snake_case
- 必填/可选字段标注清楚

---

## 输出

> Agent 完成本阶段后，必须将产出写入以下路径。

| # | 写入路径 | 内容 | 下游阶段 |
|---|---------|------|---------|
| 1 | `coding-assistant/tools/03_接口契约/api.md`（一体化）或 `coding-assistant/tools/03_接口契约/openapi.yaml`（分离） | 接口契约：请求/响应字段、类型、错误码 | SOP-05, SOP-06, SOP-08 |
| 2 | `coding-assistant/tools/99_运行日志/产出索引.md` | 更新 SOP-04 行（状态+产出文件） | — |
| 3 | `coding-assistant/tools/99_运行日志/当前状态.md` | 更新阶段进度 + 下一步 | — |
