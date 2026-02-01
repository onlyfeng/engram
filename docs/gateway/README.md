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

## Public API 导入指南

> **SSOT**: 完整的导出项分析和稳定性承诺见 [docs/architecture/gateway_public_api_surface.md](../architecture/gateway_public_api_surface.md)

Gateway 公共 API 采用 **Tier 分层**策略，插件作者应只从 `engram.gateway.public_api` 导入：

| Tier | 名称 | 稳定性承诺 | 导入方式 |
|------|------|-----------|----------|
| **A** | 核心稳定层 | 主版本内接口不变 | 直接导入 |
| **B** | 可选依赖层 | 主版本内接口不变 | **需要 try/except** |
| **C** | 便捷/内部层 | 可能在次版本调整 | 避免使用 |

### Tier A 示例（推荐）

```python
from engram.gateway.public_api import (
    # ✅ Tier A: Protocol（依赖抽象，便于测试 mock）
    RequestContext,
    GatewayDeps,
    GatewayDepsProtocol,
    WriteAuditPort,
    UserDirectoryPort,
    ActorPolicyConfigPort,
    # ✅ Tier A: 工具端口
    ToolExecutorPort,
    ToolRouterPort,
    ToolDefinition,
    ToolCallContext,
    ToolCallResult,
    # ✅ Tier A: 错误码
    McpErrorCode,
    McpErrorCategory,
    McpErrorReason,
    ToolResultErrorCode,
)

# 定义自定义 handler
async def my_handler(
    ctx: RequestContext,
    deps: GatewayDepsProtocol,  # ← 使用 Protocol 而非实现类
) -> dict:
    ...
```

### Tier B 示例（需要 try/except）

Tier B 符号依赖外部模块，导入时可能抛出 `ImportError`。**可复制代码片段**：

```python
# ⚠️ Tier B 符号依赖外部模块，导入时可能抛出 ImportError
try:
    from engram.gateway.public_api import LogbookAdapter, get_adapter
    LOGBOOK_AVAILABLE = True
except ImportError:
    LOGBOOK_AVAILABLE = False
    LogbookAdapter = None  # type: ignore[misc, assignment]

# 在代码中检查
if not LOGBOOK_AVAILABLE:
    raise RuntimeError(
        "此插件需要 engram_logbook 模块。\n"
        '请安装：pip install -e ".[full]" 或 pip install engram-logbook'
    )
```

**ImportError 消息格式契约**（Tier B 导入失败时的错误消息必须包含以下字段）：

```
ImportError: 无法导入 '{symbol_name}'（来自 {module_path}）

原因: {original_error}

{install_hint}
```

| 字段 | 说明 | 示例 |
|------|------|------|
| `symbol_name` | 导入失败的符号名 | `LogbookAdapter` |
| `module_path` | 来源模块的相对路径 | `.logbook_adapter` |
| `original_error` | 原始 ImportError 的消息文本 | `No module named 'engram_logbook'` |
| `install_hint` | 包含具体安装命令的指引 | `pip install -e ".[full]"` |

### Tier C 示例（建议避免）

> **为什么建议避免 Tier C？**
>
> - **稳定性承诺弱**：Tier C 符号可能在**次版本**中调整签名或行为
> - **隐式逻辑**：便捷函数封装了内部实现细节，升级时可能产生意外行为
> - **可替代性强**：每个 Tier C 函数都有对应的 Tier A 替代方案

| Tier C 符号 | 替代写法（Tier A） |
|-------------|-------------------|
| `create_request_context(...)` | `RequestContext(correlation_id=..., actor_user_id=...)` |
| `create_gateway_deps(...)` | `GatewayDeps(config=..., ...)` |
| `generate_correlation_id()` | 由中间件自动生成，插件无需手动调用 |

**替代写法示例**：

```python
# ❌ 避免：使用 Tier C 便捷函数
from engram.gateway.public_api import create_request_context
ctx = create_request_context(actor_user_id="user-001")

# ✅ 推荐：直接使用 Tier A 数据类构造
from engram.gateway.public_api import RequestContext
ctx = RequestContext(
    correlation_id="corr-abc123",  # 显式指定，便于追踪
    actor_user_id="user-001",
)
```

### 验证导入

```bash
# 验证 public_api 模块可正常导入（Tier A）
python -c "from engram.gateway.public_api import RequestContext, McpErrorCode; print('OK')"

# 运行 public_api 导入契约测试
pytest tests/gateway/test_public_api_import_contract.py -q
```

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
| [MCP Server 添加方法][cursor-mcp-install] | 如何在 Cursor 中添加 MCP Server |
| [MCP Server 目录][mcp-directory] | 社区 MCP Server 列表 |
| [Cursor Rules 配置][cursor-rules] | 自定义 Agent 行为规则 |
| [Cursor Agent 模式][cursor-agent] | Agent 模式下的 MCP 工具调用行为 |

> **SSOT 声明**：本仓库 MCP 配置以 `configs/mcp/.mcp.json.example` 为权威来源，外部链接仅作行为参考。
>
> **Cursor Rules 映射**：本仓库 `AGENTS.md` 作为 Workspace Rules 自动加载，定义 AI Agent 协作规范。详见 [Cursor Rules 与本仓库映射](02_mcp_integration_cursor.md#cursor-rules-与本仓库映射)。
>
> **注意**：外部链接可能随上游更新而变化。如发现失效链接，请参考 [modelcontextprotocol.io](https://modelcontextprotocol.io) 获取最新规范。

[mcp-spec]: https://modelcontextprotocol.io/specification "MCP Protocol Specification"
[mcp-transport]: https://modelcontextprotocol.io/specification/2025-03-26/basic/transports "MCP Transports"
[cursor-mcp]: https://docs.cursor.com/context/model-context-protocol "Cursor MCP Documentation"
[cursor-mcp-install]: https://docs.cursor.com/context/model-context-protocol#adding-mcp-servers "Adding MCP Servers"
[mcp-directory]: https://cursor.directory/ "MCP Server Directory"
[cursor-rules]: https://docs.cursor.com/context/rules-for-ai "Cursor Rules for AI"
[cursor-agent]: https://docs.cursor.com/chat/agent "Cursor Agent Mode"

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

更新时间：2026-02-02（添加 Cursor Agent 模式链接，补充 Cursor Rules 映射说明）
