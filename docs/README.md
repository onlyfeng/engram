# Engram 文档中心

本目录为 Engram 项目的**单一权威文档源**（Single Source of Truth）。

---

## 术语约定

| 术语 | 说明 |
|------|------|
| **Logbook** | 事实账本层（PostgreSQL 存储），记录所有事实和事件 |
| **Gateway** | MCP 网关（连接 Cursor IDE 与 OpenMemory），提供策略校验和审计 |
| **OpenMemory** | 外部语义记忆服务（独立部署），用于 AI 记忆存储和检索 |

**组件命名**：使用语义化名称 `Logbook`（事实账本）、`Gateway`（记忆网关）。详见 [命名规范](architecture/naming.md)。

---

## 文档导航

### 快速开始

| 入口 | 说明 | 文档 |
|------|------|------|
| **安装指南** | 本地安装 | [installation.md](installation.md) |
| **快速开始** | 最小部署示例 | [README.md](../README.md#快速开始) |
| **环境配置** | 环境变量参考 | [环境变量参考](reference/environment_variables.md) |

### 组件文档

| 模块 | 索引入口 | 说明 |
|------|----------|------|
| **Logbook** | [`logbook/README.md`](logbook/README.md) | 事实账本：架构、API、部署 |
| **Gateway** | [`gateway/README.md`](gateway/README.md) | MCP 网关：策略、审计、降级 |

### 架构与契约

| 分类 | 说明 | 路径 |
|------|------|------|
| **架构决策** | ADR、命名规范 | [`architecture/`](architecture/) |
| **组件契约** | 接口定义、数据流 | [`contracts/`](contracts/) |

---

## 参考文档

| 文档 | 说明 |
|------|------|
| [环境变量参考](reference/environment_variables.md) | 按组件分类的环境变量、默认值 |
| [集成指南](guides/integrate_existing_project.md) | 如何集成到现有项目 |
| [最小安全清单](guides/security_minimal.md) | 内网部署的安全与备份基线 |

---

## 组件间契约

| 契约 | 说明 |
|------|------|
| [gateway_logbook_boundary.md](contracts/gateway_logbook_boundary.md) | Gateway ↔ Logbook 边界与数据流 |
| [gateway_policy_v2.md](contracts/gateway_policy_v2.md) | Gateway 策略规范 |
| [evidence_packet.md](contracts/evidence_packet.md) | 证据包契约 |
| [outbox_lease_v2.md](contracts/outbox_lease_v2.md) | Outbox 租约协议 |
| [mcp_jsonrpc_error_v2.md](contracts/mcp_jsonrpc_error_v2.md) | MCP JSON-RPC 错误规范 |

---

## 目录结构

```
docs/
├── installation.md          # 安装指南
├── README.md                 # 本文件
├── logbook/                  # Logbook 文档
├── gateway/                  # Gateway 文档
├── architecture/             # 架构决策
├── contracts/                # 组件契约
├── guides/                   # 集成指南
└── reference/                # 参考文档
```

---

## 测试与标记

项目使用 pytest 标记来区分测试级别，标记在 `pyproject.toml` 的 `tool.pytest.ini_options` 中注册：

- `unit`：单元测试（`pytest -m unit`）
- `integration`：集成测试（`pytest -m integration`）

常见组合用法：

- 只跑单元：`pytest -m unit`
- 排除集成：`pytest -m "not integration"`

---

## 文档贡献指南

### 文档风格

- 中文为主，技术术语保留英文
- 组件名称遵循 [命名规范](architecture/naming.md)
- 代码块标注语言类型
- 表格优于长段落

### 新增文档检查

1. 是否已有文档覆盖该主题？
2. 应放在哪个目录下？
3. 是否需要更新本索引文件？
