# Architecture 文档目录

> **适用人群**：内部开发者、架构师、需要理解设计决策的贡献者

---

## 阅读顺序

| 顺序 | 文档 | 说明 |
|------|------|------|
| 1 | [naming.md](naming.md) | 命名规范：组件命名、禁止词、环境变量规范 |
| 2 | [optional_layers_integration.md](optional_layers_integration.md) | **Optional Layers 接入规范**：约束、目录结构、开关命名、验证语义 |
| 3 | [adr_docs_information_architecture.md](adr_docs_information_architecture.md) | ADR：文档信息架构与边界决策 |
| 4 | [adr_seekdb_schema_role_naming.md](adr_seekdb_schema_role_naming.md) | ADR：SeekDB Schema/Role 命名统一 |
| 5 | [legacy_naming_governance.md](legacy_naming_governance.md) | 旧组件命名治理规范 |
| 6 | [docs_legacy_retention_policy.md](docs_legacy_retention_policy.md) | 文档遗留资产保留策略 |
| 7 | [seekdb_schema_role_naming_audit.md](seekdb_schema_role_naming_audit.md) | SeekDB 命名迁移审计记录 |
| 8 | [iteration_2_plan.md](iteration_2_plan.md) | **Iteration 2 计划**：脚本收敛、SQL 整理、CI 硬化、Gateway 模块化、文档对齐 |

---

## 文档分类

### 架构决策记录（ADR）

| ADR | 主题 | 状态 |
|-----|------|------|
| [adr_docs_information_architecture.md](adr_docs_information_architecture.md) | 文档信息架构与边界决策 | Accepted |
| [adr_seekdb_schema_role_naming.md](adr_seekdb_schema_role_naming.md) | SeekDB Schema/Role 命名统一 | Accepted |

### 命名与治理规范

| 文档 | 说明 |
|------|------|
| [naming.md](naming.md) | 组件命名约束、禁止词列表、环境变量规范 |
| [optional_layers_integration.md](optional_layers_integration.md) | **Optional Layers 接入规范**：可选层约束、Compose Override、新增检查清单 |
| [legacy_naming_governance.md](legacy_naming_governance.md) | 旧组件命名治理规范 |
| [docs_legacy_retention_policy.md](docs_legacy_retention_policy.md) | 文档遗留资产保留策略 |

### 审计与迁移记录

| 文档 | 说明 |
|------|------|
| [seekdb_schema_role_naming_audit.md](seekdb_schema_role_naming_audit.md) | SeekDB 命名迁移审计 |

### 迭代计划

| 文档 | 说明 | 状态 |
|------|------|------|
| [iteration_2_plan.md](iteration_2_plan.md) | Iteration 2：代码质量与工程规范化 | 进行中 |

---

## 与其他文档的关系

### 组件契约

架构决策影响组件间契约设计：

| 契约 | 相关 ADR/规范 |
|------|---------------|
| [contracts/gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md) | naming.md |
| [contracts/evidence_packet.md](../contracts/evidence_packet.md) | naming.md |

### 组件文档

各组件文档需遵循架构规范：

| 组件 | 文档路径 | 主要约束 |
|------|----------|----------|
| Logbook | [docs/logbook/](../logbook/) | naming.md |
| Gateway | [docs/gateway/](../gateway/) | naming.md |
| SeekDB | [docs/seekdb/](../seekdb/) | naming.md, adr_seekdb_schema_role_naming.md |
| OpenMemory | [docs/openmemory/](../openmemory/) | naming.md |

---

## 快速链接

| 类型 | 链接 |
|------|------|
| 组件契约 | [docs/contracts/](../contracts/) |
| 环境变量 | [docs/reference/environment_variables.md](../reference/environment_variables.md) |
| 文档中心 | [docs/README.md](../README.md) |

---

更新时间：2026-01-31
