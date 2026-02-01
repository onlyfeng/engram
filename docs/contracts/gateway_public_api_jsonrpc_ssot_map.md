# Gateway Public API / JSON-RPC SSOT 地图

> 版本: v1.0  
> 创建日期: 2026-02-02  
> 状态: Active  
> 适用于: `src/engram/gateway/`

## 概述

本文档是 Gateway Public API 与 JSON-RPC 错误模型的**单一事实来源（SSOT）地图**，集中定义：

1. 对外承诺的符号清单与 Tier 分层
2. JSON-RPC 错误模型的权威层级
3. correlation_id 单一来源规则
4. 变更流程与同步更新清单

---

## 0. 权威来源层级

本文档作为 **索引与变更清单**，其符号清单的权威来源遵循以下层级：

```
           ┌─────────────────────────────────────────────────┐
           │           代码实现 (最高权威)                     │
           │  src/engram/gateway/public_api.py:__all__        │
           └───────────────────────┬─────────────────────────┘
                                   │ 描述
           ┌───────────────────────▼─────────────────────────┐
           │           导出项分析文档 (详细规范)               │
           │  docs/architecture/gateway_public_api_surface.md │
           └───────────────────────┬─────────────────────────┘
                                   │ 索引
           ┌───────────────────────▼─────────────────────────┐
           │           本文档 (SSOT 地图)                      │
           │  - 提供符号清单索引与快速参考                      │
           │  - 记录变更流程与同步更新清单                      │
           │  - 不作为符号定义的权威来源                        │
           └───────────────────────┬─────────────────────────┘
                                   │ 决策记录
           ┌───────────────────────▼─────────────────────────┐
           │           ADR (架构决策记录)                      │
           │  adr_gateway_public_api_jsonrpc_surface.md       │
           │  - 仅记录设计决策与历史原因                        │
           │  - 不维护完整符号清单                             │
           └─────────────────────────────────────────────────┘
```

### 0.1 各文档职责边界

| 文档 | 职责 | 维护符号清单 |
|------|------|-------------|
| `public_api.py:__all__` | 运行时导出定义（最高权威） | ✅ 唯一来源 |
| `gateway_public_api_surface.md` | 导出项详细分析、导入模式、Tier 分层规范 | ⚠️ 引用 `__all__` |
| 本文档 (SSOT map) | 索引参考、变更流程、CI 门禁映射 | ⚠️ 引用 `__all__` |
| ADR | 架构决策记录、设计原因 | ❌ 不维护 |

### 0.2 向后兼容承诺

> **完整策略**：参见 [gateway_contract_convergence.md §11](./gateway_contract_convergence.md#11-public-api-向后兼容策略)

本文档中的符号清单章节（§1）仅作为快速参考索引，实际承诺以上述契约文档为准。

---

## 1. 对外承诺符号清单

> **权威来源**：以下符号清单仅作为快速参考索引。
> - **符号定义**：以 `src/engram/gateway/public_api.py:__all__` 为准
> - **详细分析**：参见 [gateway_public_api_surface.md](../architecture/gateway_public_api_surface.md)
> - **兼容承诺**：参见 [gateway_contract_convergence.md §11](./gateway_contract_convergence.md#11-public-api-向后兼容策略)

### 1.1 Tier 分层定义

| Tier | 名称 | 导入方式 | 稳定性承诺 | 失败时行为 |
|------|------|----------|-----------|-----------|
| **A** | 核心稳定层 | 直接导入 | 主版本内接口不变 | 不适用（无外部依赖） |
| **B** | 可选依赖层 | 延迟导入 | 主版本内接口不变 | `ImportError` + 安装指引 |
| **C** | 便捷/内部层 | 直接导入 | 可能在次版本调整签名 | 不适用（无外部依赖） |

### 1.2 Tier A 符号清单（核心稳定层）

> **来源文件**: `src/engram/gateway/public_api.py`  
> **稳定性**: 主版本内接口不变，插件作者优先依赖

| 符号 | 类型 | 来源模块 | 说明 |
|------|------|----------|------|
| `RequestContext` | dataclass | `di.py` | 请求上下文 |
| `GatewayDeps` | dataclass | `di.py` | 依赖容器实现类 |
| `GatewayDepsProtocol` | Protocol | `di.py` | 依赖容器协议（推荐用于类型注解） |
| `WriteAuditPort` | Protocol | `services/ports.py` | 审计写入接口 |
| `UserDirectoryPort` | Protocol | `services/ports.py` | 用户目录接口 |
| `ActorPolicyConfigPort` | Protocol | `services/ports.py` | Actor 策略配置接口 |
| `ToolExecutorPort` | Protocol | `services/ports.py` | 工具执行器端口 |
| `ToolRouterPort` | Protocol | `services/ports.py` | 工具路由器端口 |
| `ToolDefinition` | dataclass | `services/ports.py` | 工具定义 |
| `ToolCallContext` | class | `services/ports.py` | 工具调用上下文 |
| `ToolCallResult` | class | `services/ports.py` | 工具调用结果 |
| `McpErrorCode` | class | `error_codes.py` | JSON-RPC 错误码常量 |
| `McpErrorCategory` | class | `error_codes.py` | 错误分类常量 |
| `McpErrorReason` | class | `error_codes.py` | 错误原因码常量 |
| `ToolResultErrorCode` | class | `result_error_codes.py` | 工具执行结果错误码 |

### 1.3 Tier B 符号清单（可选依赖层）

> **来源文件**: `src/engram/gateway/public_api.py` (`_TIER_B_LAZY_IMPORTS`)  
> **稳定性**: 主版本内接口不变，失败时抛出 `ImportError` + 安装指引

| 符号 | 类型 | 来源模块 | 外部依赖 |
|------|------|----------|----------|
| `LogbookAdapter` | class | `logbook_adapter.py` | engram_logbook |
| `get_adapter` | function | `logbook_adapter.py` | engram_logbook |
| `get_reliability_report` | function | `logbook_adapter.py` | engram_logbook |
| `execute_tool` | function | `entrypoints/tool_executor.py` | Gateway 完整依赖 |
| `dispatch_jsonrpc_request` | function | `mcp_rpc.py` | MCP RPC 模块 |
| `JsonRpcDispatchResult` | Pydantic 模型 | `mcp_rpc.py` | MCP RPC 模块 |

### 1.4 Tier C 符号清单（便捷/内部层）

> **来源文件**: `src/engram/gateway/public_api.py`  
> **稳定性**: 可能在次版本调整签名，建议使用 Tier A 替代方案

| 符号 | 类型 | 来源模块 | Tier A 替代方案 |
|------|------|----------|-----------------|
| `create_request_context` | function | `di.py` | `RequestContext(...)` |
| `create_gateway_deps` | function | `di.py` | `GatewayDeps(...)` |
| `generate_correlation_id` | function | `di.py` | 通常由中间件自动生成 |

---

## 2. JSON-RPC 错误模型 SSOT 层级

### 2.1 权威来源层级图

```
           ┌─────────────────────────────────────────┐
           │        JSON Schema (最高权威)            │
           │ schemas/mcp_jsonrpc_error_v1.schema.json │
           └────────────────┬────────────────────────┘
                            │ 校验
           ┌────────────────▼────────────────────────┐
           │          代码实现 (行为权威)              │
           │  src/engram/gateway/error_codes.py      │
           │  - McpErrorCode                         │
           │  - McpErrorCategory                     │
           │  - McpErrorReason                       │
           └────────────────┬────────────────────────┘
                            │ 验证
           ┌────────────────▼────────────────────────┐
           │          契约测试 (契约锚点)              │
           │  tests/gateway/test_mcp_jsonrpc_contract.py │
           │  - TestErrorReasonWhitelistConsistency  │
           │  - TestErrorCodeConstants               │
           └────────────────┬────────────────────────┘
                            │ 描述
           ┌────────────────▼────────────────────────┐
           │          文档 (最终派生)                  │
           │  docs/contracts/mcp_jsonrpc_error_v1.md │
           └─────────────────────────────────────────┘
```

### 2.2 error.data.reason 三源 SSOT

`error.data.reason` 字段的有效值由三个来源共同定义，CI 门禁确保一致性：

| 权威来源 | 路径 | 作用 |
|----------|------|------|
| **JSON Schema** | `schemas/mcp_jsonrpc_error_v1.schema.json` 的 `definitions.error_reason.enum` | 结构验证的最高权威 |
| **代码实现** | `src/engram/gateway/error_codes.py:McpErrorReason` | 运行时常量定义 |
| **门禁脚本** | `scripts/ci/check_mcp_jsonrpc_error_contract.py` | 双向验证一致性 |

**验证命令**：

```bash
make check-mcp-error-contract
```

### 2.3 SSOT 文件索引

| 契约元素 | 权威文件 | 说明 |
|----------|----------|------|
| ErrorData 结构 | `schemas/mcp_jsonrpc_error_v1.schema.json` | JSON-RPC error.data 结构定义 |
| ErrorCode 枚举 | `src/engram/gateway/error_codes.py:McpErrorCode` | -32xxx 错误码常量 |
| ErrorCategory 枚举 | `src/engram/gateway/error_codes.py:McpErrorCategory` | 错误分类常量 |
| ErrorReason 枚举 | `src/engram/gateway/error_codes.py:McpErrorReason` | 错误原因码常量 |
| 审计事件结构 | `schemas/audit_event_v1.schema.json` | AuditEvent 结构定义 |
| Outbox reason 码 | `src/engram/logbook/errors.py:ErrorCode` | Outbox 审计 reason 定义 |

---

## 3. correlation_id 单一来源规则

### 3.1 单一来源模块

| 属性 | 值 |
|------|------|
| **SSOT 模块** | `src/engram/gateway/correlation_id.py` |
| **格式规范** | `^corr-[a-fA-F0-9]{16}$` |
| **长度** | 21 字符 (5 + 16) |
| **示例** | `corr-a1b2c3d4e5f67890` |

### 3.2 核心函数

| 函数 | 说明 | 使用场景 |
|------|------|----------|
| `generate_correlation_id()` | 生成新的 correlation_id | 中间件请求入口 |
| `is_valid_correlation_id(str)` | 校验格式是否合规 | 输入校验 |
| `normalize_correlation_id(str)` | 归一化（不合规则重新生成） | 外部传入值处理 |
| `CORRELATION_ID_PATTERN` | 正则表达式常量 | Schema 对齐校验 |

### 3.3 使用规则

| 规则 | 说明 |
|------|------|
| **统一来源** | 所有 correlation_id 的生成、校验、归一化都应通过 `correlation_id.py` |
| **禁止硬编码** | 禁止在其他模块中直接使用 `f"corr-{uuid.uuid4().hex[:16]}"` |
| **格式对齐** | 必须与 `schemas/audit_event_v1.schema.json` 中的格式定义一致 |

### 3.4 门禁与测试锚点

| 类型 | 文件 | 说明 |
|------|------|------|
| **门禁脚本** | `scripts/ci/check_gateway_correlation_id_single_source.py` | 检查 correlation_id 生成是否来自单一来源 |
| **Makefile 目标** | `make check-gateway-correlation-id-single-source` | CI 门禁命令 |
| **契约测试** | `tests/gateway/test_mcp_jsonrpc_contract.py::TestCorrelationIdSingleSourceContract` | correlation_id 格式与传递契约 |
| **契约测试** | `tests/gateway/test_mcp_jsonrpc_contract.py::TestCorrelationIdHeaderAlignment` | 响应 Header 对齐 |
| **契约测试** | `tests/gateway/test_audit_event_contract.py::TestEvidenceRefsJsonCorrelationIdConsistencyContract` | 审计记录一致性 |
| **门禁测试** | `tests/ci/test_gateway_correlation_id_single_source_gate.py` | 门禁脚本单元测试 |

---

## 4. 变更流程与同步更新清单

### 4.1 变更类型与必须同步更新的文件

#### 4.1.1 新增/修改 Tier A 符号

| 步骤 | 文件 | 说明 | 对应 make gate |
|------|------|------|----------------|
| 1 | `src/engram/gateway/public_api.py` | 更新 `__all__` 导出列表 | `make check-gateway-public-api-surface` |
| 2 | `docs/architecture/gateway_public_api_surface.md` | 更新导出项文档 | `make check-gateway-public-api-docs-sync` |
| 3 | `tests/gateway/test_public_api_exports.py` | 更新导出测试 | `make test` |
| 4 | 本文档 (§1.2) | 更新符号清单 | - |

#### 4.1.2 新增/修改 Tier B 符号

| 步骤 | 文件 | 说明 | 对应 make gate |
|------|------|------|----------------|
| 1 | `src/engram/gateway/public_api.py` | 更新 `_TIER_B_LAZY_IMPORTS` | `make check-gateway-public-api-import-surface` |
| 2 | `src/engram/gateway/public_api.py` | 更新 `_TIER_B_INSTALL_HINTS` | - |
| 3 | `docs/architecture/gateway_public_api_surface.md` | 更新 Tier B 文档 | `make check-gateway-public-api-docs-sync` |
| 4 | `tests/gateway/test_public_api_import_contract.py` | 更新 Tier B 测试 | `make test` |
| 5 | 本文档 (§1.3) | 更新符号清单 | - |

#### 4.1.3 新增/修改 ErrorReason

| 步骤 | 文件 | 说明 | 对应 make gate |
|------|------|------|----------------|
| 1 | `schemas/mcp_jsonrpc_error_v1.schema.json` | 更新 `error_reason.enum` | `make check-schemas` |
| 2 | `src/engram/gateway/error_codes.py` | 更新 `McpErrorReason` | `make check-mcp-error-contract` |
| 3 | `docs/contracts/mcp_jsonrpc_error_v1.md` | 更新错误码文档 | - |
| 4 | `tests/gateway/test_mcp_jsonrpc_contract.py` | 更新契约测试 | `make test` |

#### 4.1.4 修改 correlation_id 相关逻辑

| 步骤 | 文件 | 说明 | 对应 make gate |
|------|------|------|----------------|
| 1 | `src/engram/gateway/correlation_id.py` | SSOT 实现模块 | `make check-gateway-correlation-id-single-source` |
| 2 | `schemas/audit_event_v1.schema.json` | 格式定义（如需） | `make check-schemas` |
| 3 | `tests/gateway/test_mcp_jsonrpc_contract.py` | 更新契约测试 | `make test` |
| 4 | 本文档 (§3) | 更新规则说明 | - |

### 4.2 最小验收命令集

> 修改 Public API 或 JSON-RPC 相关代码后，应运行以下命令：

```bash
# 1. Public API 导入表面检查
python scripts/ci/check_gateway_public_api_import_surface.py

# 2. Public API 文档同步检查
python scripts/ci/check_gateway_public_api_docs_sync.py

# 3. MCP 错误码合约检查
python scripts/ci/check_mcp_jsonrpc_error_contract.py

# 4. correlation_id 单一来源检查
python scripts/ci/check_gateway_correlation_id_single_source.py

# 5. 契约测试
pytest tests/gateway/test_public_api_import_contract.py \
       tests/gateway/test_mcp_jsonrpc_contract.py \
       tests/gateway/test_public_api_exports.py -q
```

**单行执行（推荐）**：

```bash
make check-gateway-public-api-surface && \
make check-gateway-public-api-docs-sync && \
make check-mcp-error-contract && \
make check-gateway-correlation-id-single-source && \
pytest tests/gateway/test_public_api_import_contract.py tests/gateway/test_mcp_jsonrpc_contract.py -q
```

### 4.3 CI Job 对应关系

| 验收命令 | CI Job | 说明 |
|----------|--------|------|
| `check_gateway_public_api_import_surface.py` | `gateway-public-api-surface` | Tier B 延迟导入策略 |
| `check_gateway_public_api_docs_sync.py` | `gateway-public-api-docs-sync` | 文档同步 |
| `check_mcp_jsonrpc_error_contract.py` | `mcp-error-contract` | Schema ↔ 代码一致性 |
| `check_gateway_correlation_id_single_source.py` | `gateway-correlation-id` | 单一来源检查 |
| `test_public_api_import_contract.py` | `test` | Tier A/B 分层契约 |
| `test_mcp_jsonrpc_contract.py` | `test` | JSON-RPC 契约测试 |

---

## 5. 相关文档

| 文档 | 路径 | 说明 |
|------|------|------|
| Gateway Public API Surface | [gateway_public_api_surface.md](../architecture/gateway_public_api_surface.md) | 导出项详细分析 |
| Gateway 契约收敛文档 | [gateway_contract_convergence.md](./gateway_contract_convergence.md) | 五域契约汇总 |
| MCP JSON-RPC 错误模型契约 | [mcp_jsonrpc_error_v1.md](./mcp_jsonrpc_error_v1.md) | 错误模型详细规范 |
| Gateway Public API JSON-RPC Surface ADR | [adr_gateway_public_api_jsonrpc_surface.md](../architecture/adr_gateway_public_api_jsonrpc_surface.md) | 架构决策记录 |
| Gateway ImportError 规范 | [gateway_importerror_and_optional_deps.md](../architecture/gateway_importerror_and_optional_deps.md) | 可选依赖错误处理 |

---

## 6. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-02-02 | 初始版本：符号清单、SSOT 层级、correlation_id 规则、变更流程 |
| v1.1 | 2026-02-02 | 新增 §0 权威来源层级：明确符号清单以 `public_api.__all__` 为准，ADR 仅记录决策，本文档仅给出索引与变更清单 |
