# Logbook 文档目录

> **适用人群**：内部开发者、需要深入了解 Logbook 实现的集成方

---

## 阅读顺序

| 顺序 | 文档 | 说明 |
|------|------|------|
| 1 | [00_overview.md](00_overview.md) | 概览：Logbook 解决什么问题、与其他组件的边界 |
| 2 | [01_architecture.md](01_architecture.md) | 架构：Schema 设计、SCM 同步机制、大规模并行同步 |
| 3 | [02_tools_contract.md](02_tools_contract.md) | 工具契约：CLI、脚本接口、数据流 |
| 4 | [03_deploy_verify_troubleshoot.md](03_deploy_verify_troubleshoot.md) | 部署验收：最小部署、验收测试、常见问题排错 |
| 5 | [04_acceptance_criteria.md](04_acceptance_criteria.md) | **验收标准**：MVP 能力清单、不变量约束、验收矩阵 |
| 6 | [05_definition_of_done.md](05_definition_of_done.md) | **DoD**：变更类型与必须同步更新的文件、破坏性变更要求 |

---

## 模块边界

### 相关契约

| 契约 | 说明 |
|------|------|
| [gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md) | Gateway ↔ Logbook 边界与数据流 |
| [evidence_packet.md](../contracts/evidence_packet.md) | 证据包契约（与 SeekDB 共用） |
| [outbox_lease_v1.md](../contracts/outbox_lease_v1.md) | Outbox 租约协议 |

### 相关组件文档

| 组件 | 文档路径 | 关系 |
|------|----------|------|
| Gateway | [docs/gateway/](../gateway/) | Gateway 通过 Logbook 落库审计事件 |
| SeekDB | [docs/seekdb/](../seekdb/) | SeekDB 索引 Logbook 中的证据 |

---

## 开发者入口

- **模块路径**: `apps/logbook_postgres/`
- **Python 包**: `engram_logbook`
- **CLI 命令**: `logbook` / `engram-logbook`
- **开发者指南**: [`apps/logbook_postgres/README.md`](../../apps/logbook_postgres/README.md)

---

## 快速链接

| 类型 | 链接 |
|------|------|
| 快速开始 | [README.md#快速开始](../../README.md#快速开始) |
| 环境变量 | [docs/reference/environment_variables.md](../reference/environment_variables.md) |
| 命名规范 | [docs/architecture/naming.md](../architecture/naming.md) |
| 文档中心 | [docs/README.md](../README.md) |

---

更新时间：2026-01-30
