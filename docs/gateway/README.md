# Gateway 文档目录

> **适用人群**：内部开发者、需要深入了解 MCP 网关实现的集成方

---

## 阅读顺序

| 顺序 | 文档 | 说明 |
|------|------|------|
| 1 | [00_overview.md](00_overview.md) | 概览：Gateway 解决什么问题、OpenMemory 依赖面 |
| 2 | [06_gateway_design.md](06_gateway_design.md) | Gateway 设计：策略引擎、降级机制 |
| 3 | [03_memory_contract.md](03_memory_contract.md) | 记忆契约：字段映射、API 路径 |
| 4 | [04_governance_switch.md](04_governance_switch.md) | 治理开关：团队写入策略 |
| 5 | [05_failure_degradation.md](05_failure_degradation.md) | 失败降级：Outbox 机制、重试策略 |
| 6 | [01_openmemory_deploy_windows.md](01_openmemory_deploy_windows.md) | Windows 部署指南（可选） |
| 7 | [02_mcp_integration_cursor.md](02_mcp_integration_cursor.md) | Cursor MCP 集成指南 |

---

## 模块边界

### 相关契约

| 契约 | 说明 |
|------|------|
| [gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md) | Gateway ↔ Logbook 边界与数据流 |
| [evidence_packet.md](../contracts/evidence_packet.md) | 证据包契约 |
| [outbox_lease_v1.md](../contracts/outbox_lease_v1.md) | Outbox 租约协议（降级写入） |

### 相关组件文档

| 组件 | 文档路径 | 关系 |
|------|----------|------|
| Logbook | [docs/logbook/](../logbook/) | Gateway 审计事件落库到 Logbook |
| OpenMemory | [docs/openmemory/](../openmemory/) | Gateway 作为 OpenMemory 的 MCP 代理层 |

---

## 开发者入口

- **模块路径**: `src/engram/gateway/`
- **Python 包**: `engram.gateway`
- **服务端口**: `8787`（默认）
- **开发者指南**: [docs/gateway/06_gateway_design.md](06_gateway_design.md)

---

## 快速链接

| 类型 | 链接 |
|------|------|
| MCP 配置 | [README.md#mcp-配置cursoride-集成](../../README.md#mcp-配置cursoride-集成) |
| 健康检查 | [README.md#健康检查](../../README.md#健康检查) |
| 统一栈验证 | [README.md#统一栈验证入口](../../README.md#统一栈验证入口) |
| 环境变量 | [docs/reference/environment_variables.md](../reference/environment_variables.md) |
| 命名规范 | [docs/architecture/naming.md](../architecture/naming.md) |
| 文档中心 | [docs/README.md](../README.md) |

---

## 外部参考

Gateway 实现遵循 MCP (Model Context Protocol) 规范，以下为关键外部文档：

| 资源 | 说明 |
|------|------|
| [MCP 协议规范][mcp-spec] | 核心协议定义，JSON-RPC 消息格式 |
| [MCP 传输层规范][mcp-transport] | HTTP 传输、CORS、Session 管理 |
| [Cursor MCP 文档][cursor-mcp] | IDE 集成配置指南 |

> **注意**：外部链接可能随上游更新而变化。如发现失效链接，请参考 [modelcontextprotocol.io](https://modelcontextprotocol.io) 获取最新规范。

[mcp-spec]: https://modelcontextprotocol.io/specification "MCP Protocol Specification"
[mcp-transport]: https://modelcontextprotocol.io/specification/2025-03-26/basic/transports "MCP Transports"
[cursor-mcp]: https://docs.cursor.com/context/model-context-protocol "Cursor MCP Documentation"

---

## 验证入口

**推荐**：通过 Makefile 执行统一栈验证：

```bash
make verify-unified                    # 基础验证
VERIFY_FULL=1 make verify-unified      # 完整验证（含降级测试）
```

**备用**：若无脚本入口，直接使用 `make verify-unified`。

详细说明参见 [根 README §统一栈验证入口](../../README.md#统一栈验证入口)。

---

更新时间：2026-01-30
