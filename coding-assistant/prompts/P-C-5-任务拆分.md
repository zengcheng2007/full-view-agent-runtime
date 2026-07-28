# P-C-5 · 任务拆分

## 触发阶段
SOP-05 任务拆分

## 使用时机
接口契约就位后，按业务闭环拆分编码任务。

---

## Prompt 模板

```
Use the writing-plans skill.

请基于以下信息，按业务闭环拆分编码任务：

技术方案：[粘贴技术方案概要]

接口契约：[粘贴 api.md 或 OpenAPI 关键端点]

约束：
- 每步 2~5 分钟一个 commit 粒度
- **按业务闭环切，不按文件切**
- 每步含 [测试 → 实现 → 验证 → 提交] 四个动作
- 标注步骤间的依赖关系

拆分原则：
- 一个步骤 = 一个用户可感知的可验证行为完成
- 例如："用户可以输入关键词搜索并看到结果"是一个步骤
- 不拆成"建文件 → 加类型 → 加函数"这类纯文件操作

输出要求：
- 每步：目标（一句话）+ 涉及文件（精确路径）+ 完成标准（可验证命令或行为）+ TDD 四动作
- 步骤间依赖关系用 ASCII 箭头标注
- 风险步骤标注（第三方 API / 性能敏感 / 安全相关）

输出到 coding-assistant/tools/04_任务拆分/任务清单-[日期].md
```

---

## 拆分示例

```
Step 1: 类型定义 + 查询接口服务层
  目标：定义 SearchResult 类型，封装 searchApi 函数
  文件：src/types/search.ts, src/services/search.ts
  完成标准：tsc --noEmit 通过，fetchSearch 函数签名正确

Step 2: 搜索 Store action
  目标：创建 useSearchStore，管理 searchQuery/result/loading/error 状态
  文件：src/stores/search.ts
  完成标准：store action 可调用，状态变更可测试
  …
```

---

## 质量要求

- 不要拆成"建文件夹"这类步骤
- 每步完成标准可独立验证
- 依赖关系不出现循环
