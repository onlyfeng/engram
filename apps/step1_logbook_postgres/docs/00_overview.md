# 概览

## Step1 解决什么问题
你当前的工作流中，Agent 通过 `grep + edit` 维护 `manifest.csv/index.md` 的方式存在典型风险：
- 并发冲突：多人/多 Agent 同时写会互相覆盖
- 无约束：缺少唯一性、外键、状态机约束，容易“写脏”
- 难追溯：缺少审计字段与证据链指针，复盘成本高
- 难统计：无法低成本做聚合、趋势、质量指标

Step1 用 Postgres 把“事实层”做成团队基础设施：
- 事件账本（append-only）+ 状态机
- 附件与证据链索引（仅指针 + hash）
- 身份映射（人 ↔ SVN/Git 账号/别名/角色标签）
- 运行记录（每次分析/同步/写入的成本、错误、版本）
- SCM 增量同步（cursor/watermark 机制，支持大规模多仓库并行同步，详见 [架构文档](01_architecture.md#scm-sync-at-scale)）

## Step1 与 Step2/Step3 的边界
- Step1：真相源（SoT），可审计、可回放、可校验
- Step2（OpenMemory）：经验/记忆层（可演化，可强化/衰减），默认团队可读；团队可写受开关与策略治理
- Step3（seekdb/RAG）：证据检索加速层（对大文本/代码/报告分块索引），与 Step2 组合实现“先策略后证据”
