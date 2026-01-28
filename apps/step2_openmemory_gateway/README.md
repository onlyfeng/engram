# Step 2：团队级记忆系统（OpenMemory + MCP + 可控写入开关）

本 Step 的目标是把“经验/流程/审查口径/坑点/反思”等**可演化知识**沉淀到 OpenMemory，
并通过 **MCP 工具**直接供 Cursor 中的 Agent 调用。

关键约束（按当前决策）：
- **团队可读默认**
- **团队可写默认由开关控制**（team_write_enabled），并支持策略约束（白名单/类型/证据链/长度）
- 为了强制治理：建议增加一层 **Memory Gateway（MCP Server）**，Cursor 只连接 Gateway；Gateway 再调用 OpenMemory 后端
- OpenMemory 数据库存储与 Step1 共用同一个 Postgres（每项目一个 DB，schema 分层）

目录结构（本 zip）：
- docs/：部署、契约、治理、降级方案
- templates/：Cursor MCP 配置、环境变量模板、记忆卡片模板
- gateway/：Gateway 设计与实现骨架（可交由 Cursor Agent 完成）

> **推荐**：配合统一栈运行（根目录 `make deploy`）。如需分步独立运行本模块，需显式配置 `POSTGRES_DSN` 和 `OPENMEMORY_BASE_URL` 环境变量。

更新时间：2026-01-26
