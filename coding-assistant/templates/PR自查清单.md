# PR 自查清单

## 改动说明
（改了啥、为啥改）

## 关联
- 需求：<链接>
- 设计/原型：<链接>
- 架构：一体化 / 分离

## 自查
- [ ] writing-plans 写过 plan
- [ ] test-driven-development 全流程红绿
- [ ] verification-before-completion 通过
- [ ] 样式规范扫过（对照《样式规范.md》）
- [ ] 无硬编码色值/间距（全部取自 `tokens.ts`）
- [ ] 接口字段对契约（分离架构）
- [ ] 改动范围 ≤ 3 个相关模块
- [ ] 测试全绿（fresh 跑，非"上次跑过"）
- [ ] lint / typecheck / build 全绿
