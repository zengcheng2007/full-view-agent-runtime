# P-C-3 · 接口契约生成

## 触发阶段
SOP-04 接口对齐

## 使用时机
技术方案人工审核通过后。

---

## 一体化架构 Prompt

```
基于以下接口设计草案，生成接口契约文档 api.md：

[粘贴技术方案中的接口设计草案]

要求：
1. 所有请求/响应字段含 TypeScript 类型和示例值
2. 标注必填/可选字段
3. 统一字段命名：接口字段 snake_case，TS 类型 camelCase
4. 列出所有可能的错误情况和处理方式
5. 输出格式参照 coding-assistant/templates/接口契约模板.md
6. 存入 coding-assistant/tools/03_接口契约/api.md
```

---

## 分离架构 Prompt

```
基于以下接口设计草案，生成 OpenAPI 3.0 契约：

[粘贴技术方案中的接口设计草案]

要求：
1. 所有请求/响应字段含类型和示例
2. 所有错误码列出（业务码 + HTTP 状态）
3. 统一字段命名：snake_case
4. 标注 required 字段
5. 输出 yaml 格式，存入 coding-assistant/tools/03_接口契约/openapi.yaml

YAML 语法铁律（违反会解析报错）：
- 禁止在双引号字符串内嵌套未转义的双引号
  错误：example: "["district","street"]"
  正确：example: '["district","street"]'  或改用 YAML 列表语法
- 复杂数据结构直接用 YAML 原生语法，不把 JSON 塞进字符串
  错误：example: "{ type: string }"
  正确：直接用 YAML mapping/sequence 表示
- 缩进只用空格，不用 Tab
- 冒号后必须有空格（key: value，不是 key:value）

注意：
- 契约 PR 必须先合，再写代码
- 字段口径对齐需求文档中的定义（名称/单位/取值范围）
```

---

## 质量要求

- 所有字段有类型 + 示例
- 所有端点有错误码
- 分页/排序/筛选参数统一规范
- **生成后必须通过 SOP-04 §校验流程 的 3 步校验，否则重新生成直到通过**
