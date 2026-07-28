# P-C-4 · 测试补充

## 触发阶段
SOP-07 自测

## 使用时机
编码完成后，对缺少测试的代码段补充测试。

---

## Prompt 模板

```
Use the test-driven-development skill.

下面这段代码需要补充单元测试：

[粘贴代码]

测试要求：

1. **正向**：正常输入返回预期结果
2. **反向**：非法输入抛出异常或返回错误
3. **边界值**：
   - 空字符串 / undefined / null
   - 数字 0 / 负数 / 极大值
   - 空数组 / 空对象
   - 特殊字符（<script>、SQL 注入字符等）
4. **异常路径**：
   - 网络超时/断开
   - API 返回 500
   - 数据格式不匹配

约束：
- 不允许 mock 业务核心逻辑
- 测试文件命名：__tests__/[原文件名].test.ts
- 使用项目配置的测试框架（[Vitest/Jest/Playwright]）
```

---

## 前端组件测试补充

```
Use the test-driven-development skill.

下面这个前端组件需要补充测试：

[粘贴组件代码]

测试要求：

1. **渲染测试**：各状态（loading/empty/error/normal）渲染正确
2. **交互测试**：点击/输入/提交触发预期回调
3. **边界测试**：props 为空/undefined/极值时的表现
4. **快照测试**：关键 UI 状态不意外变化

约束：
- 使用 [Vitest + React Testing Library / Playwright] 框架
- 不 mock 组件内部的业务 hooks
```

---

## 输出格式

贴测试运行结果：
```
PASS  __tests__/xxx.test.ts
  ✓ 正向：...
  ✓ 反向：...
  ✓ 边界：...
  ✓ 异常：...

Tests: N passed, N total
```
