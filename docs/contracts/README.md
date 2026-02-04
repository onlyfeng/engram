# Contracts 文档目录

> **适用人群**：内部开发者、组件间集成开发者、需要理解模块边界的架构师

---

## 阅读顺序

契约文档可按需查阅，建议按依赖关系阅读：

| 顺序 | 文档 | 说明 |
|------|------|------|
| 1 | [gateway_logbook_boundary.md](gateway_logbook_boundary.md) | Gateway ↔ Logbook 边界：数据流、职责划分 |
| 2 | [logbook_seekdb_boundary.md](logbook_seekdb_boundary.md) | Logbook ↔ SeekDB 边界：数据依赖、禁用开关 |
| 3 | [evidence_packet.md](evidence_packet.md) | 证据包契约：SeekDB 输出、Gateway 消费 |
| 4 | [outbox_lease_v2.md](outbox_lease_v2.md) | Outbox 租约协议：降级写入、重试机制 |
| 5 | [versioning.md](versioning.md) | 版本控制策略：Schema 迁移、API 版本 |

---

## 契约覆盖范围

### 组件边界契约

| 契约 | 涉及组件 | 边界类型 |
|------|----------|----------|
| [gateway_logbook_boundary.md](gateway_logbook_boundary.md) | Gateway ↔ Logbook | 数据流、审计落库 |
| [logbook_seekdb_boundary.md](logbook_seekdb_boundary.md) | Logbook ↔ SeekDB | 数据依赖、禁用开关 |
| [evidence_packet.md](evidence_packet.md) | SeekDB → Gateway/Agent | 数据格式、字段定义 |

### 协议契约

| 契约 | 涉及场景 | 协议类型 |
|------|----------|----------|
| [outbox_lease_v2.md](outbox_lease_v2.md) | Gateway 降级、Worker 消费 | 租约协议 |
| [versioning.md](versioning.md) | Schema 迁移、API 演进 | 版本策略 |

---

## 相关组件文档

| 组件 | 文档路径 | 涉及契约 |
|------|----------|----------|
| Logbook | [docs/logbook/](../logbook/) | gateway_logbook_boundary, outbox_lease_v2 |
| Gateway | [docs/gateway/](../gateway/) | gateway_logbook_boundary, evidence_packet, outbox_lease_v2 |
| SeekDB | [docs/seekdb/](../seekdb/) | evidence_packet |

---

## 架构决策

契约设计相关的 ADR：

| ADR | 说明 |
|-----|------|
| [adr_docs_information_architecture.md](../architecture/adr_docs_information_architecture.md) | 文档信息架构与边界决策 |
| [naming.md](../architecture/naming.md) | 命名规范（影响契约字段命名） |

---

## 快速链接

| 类型 | 链接 |
|------|------|
| 架构文档 | [docs/architecture/](../architecture/) |
| 命名规范 | [docs/architecture/naming.md](../architecture/naming.md) |
| 文档中心 | [docs/README.md](../README.md) |

---

更新时间：2026-01-30
