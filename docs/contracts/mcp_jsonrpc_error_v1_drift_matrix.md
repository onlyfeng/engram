# MCP JSON-RPC 错误模型漂移矩阵

> 版本: v1.0  
> 生效日期: 2026-02-01  
> 关联契约: [`mcp_jsonrpc_error_v1.md`](./mcp_jsonrpc_error_v1.md)

## 概述

本文档列出 MCP JSON-RPC 错误模型涉及的所有字段、枚举、常量及其权威来源与同步目标，用于追踪契约漂移和确保一致性。

---

## 1. 字段/枚举/常量漂移矩阵

### 1.1 error.data.reason（JSON-RPC 协议层错误原因码）

| 元素名 | 权威来源 (SSOT) | 同步目标 | 验证点 | 当前状态 |
|--------|----------------|----------|--------|----------|
| `PARSE_ERROR` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.1 | CI 脚本, 测试 | ✅ 一致 |
| `INVALID_REQUEST` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.1 | CI 脚本, 测试 | ✅ 一致 |
| `METHOD_NOT_FOUND` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.1 | CI 脚本, 测试 | ✅ 一致 |
| `MISSING_REQUIRED_PARAM` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.2 | CI 脚本, 测试 | ✅ 一致 |
| `INVALID_PARAM_TYPE` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.2 | CI 脚本, 测试 | ✅ 一致 |
| `INVALID_PARAM_VALUE` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.2 | CI 脚本, 测试 | ✅ 一致 |
| `UNKNOWN_TOOL` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.2 | CI 脚本, 测试 | ✅ 一致 |
| `POLICY_REJECT` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.3 | CI 脚本, 测试 | ✅ 一致 |
| `AUTH_FAILED` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.3 | CI 脚本, 测试 | ✅ 一致 |
| `ACTOR_UNKNOWN` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.3 | CI 脚本, 测试 | ✅ 一致 |
| `GOVERNANCE_UPDATE_DENIED` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.3 | CI 脚本, 测试 | ✅ 一致 |
| `OPENMEMORY_UNAVAILABLE` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.4 | CI 脚本, 测试 | ✅ 一致 |
| `OPENMEMORY_CONNECTION_FAILED` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.4 | CI 脚本, 测试 | ✅ 一致 |
| `OPENMEMORY_API_ERROR` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.4 | CI 脚本, 测试 | ✅ 一致 |
| `LOGBOOK_DB_UNAVAILABLE` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.4 | CI 脚本, 测试 | ✅ 一致 |
| `LOGBOOK_DB_CHECK_FAILED` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.4 | CI 脚本, 测试 | ✅ 一致 |
| `INTERNAL_ERROR` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.5 | CI 脚本, 测试 | ✅ 一致 |
| `TOOL_EXECUTOR_NOT_REGISTERED` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.5 | CI 脚本, 测试 | ✅ 一致 |
| `UNHANDLED_EXCEPTION` | Schema enum | McpErrorReason, ErrorReason, 文档 §4.5 | CI 脚本, 测试 | ✅ 一致 |

### 1.2 error.data.category（错误分类枚举）

| 元素名 | 权威来源 (SSOT) | 同步目标 | 验证点 | 当前状态 |
|--------|----------------|----------|--------|----------|
| `protocol` | Schema enum | McpErrorCategory, ErrorCategory, 文档 §2 | Schema 校验测试 | ✅ 一致 |
| `validation` | Schema enum | McpErrorCategory, ErrorCategory, 文档 §2 | Schema 校验测试 | ✅ 一致 |
| `business` | Schema enum | McpErrorCategory, ErrorCategory, 文档 §2 | Schema 校验测试 | ✅ 一致 |
| `dependency` | Schema enum | McpErrorCategory, ErrorCategory, 文档 §2 | Schema 校验测试 | ✅ 一致 |
| `internal` | Schema enum | McpErrorCategory, ErrorCategory, 文档 §2 | Schema 校验测试 | ✅ 一致 |

### 1.3 JSON-RPC 错误码（error.code）

| 错误码 | 名称 | 权威来源 (SSOT) | 同步目标 | 验证点 | 当前状态 |
|--------|------|----------------|----------|--------|----------|
| `-32700` | PARSE_ERROR | Schema enum | McpErrorCode, 文档 §5 | Schema 校验 | ✅ 一致 |
| `-32600` | INVALID_REQUEST | Schema enum | McpErrorCode, 文档 §5 | Schema 校验 | ✅ 一致 |
| `-32601` | METHOD_NOT_FOUND | Schema enum | McpErrorCode, 文档 §5 | Schema 校验 | ✅ 一致 |
| `-32602` | INVALID_PARAMS | Schema enum | McpErrorCode, 文档 §5 | Schema 校验 | ✅ 一致 |
| `-32603` | INTERNAL_ERROR | Schema enum | McpErrorCode, 文档 §5 | Schema 校验 | ✅ 一致 |
| `-32001` | DEPENDENCY_UNAVAILABLE | Schema enum | McpErrorCode, 文档 §5 | Schema 校验 | ✅ 一致 |
| `-32002` | BUSINESS_REJECTION | Schema enum | McpErrorCode, 文档 §5 | Schema 校验 | ✅ 一致 |
| `-32000` | TOOL_EXECUTION_ERROR | 文档 §13.5 | McpErrorCode（废弃保留） | 文档声明 | ⚠️ 已废弃 |

### 1.4 业务层错误码（result.error_code）

| 元素名 | 权威来源 (SSOT) | 同步目标 | 验证点 | 当前状态 |
|--------|----------------|----------|--------|----------|
| `DEPENDENCY_MISSING` | `result_error_codes.py:ToolResultErrorCode` | 文档 §3.2, §4.6 | 无自动验证 | ✅ 仅业务层 |
| `MISSING_REQUIRED_PARAM` | `result_error_codes.py:ToolResultErrorCode` | 文档 §4.6 | 无自动验证 | ✅ 仅业务层 |

---

## 2. 导出面/别名关系

### 2.1 类/模块导出面

| 导出名 | 真实定义位置 | 别名/重导出位置 | 说明 |
|--------|-------------|----------------|------|
| `McpErrorReason` | `error_codes.py` | — | 真实定义位置 |
| `ErrorReason` | — | `mcp_rpc.py`（别名） | `= McpErrorReason` |
| `McpErrorCategory` | `error_codes.py` | — | 真实定义位置 |
| `ErrorCategory` | — | `mcp_rpc.py`（别名） | `= McpErrorCategory` |
| `McpErrorCode` | `error_codes.py` | — | 真实定义位置 |
| `JsonRpcErrorCode` | — | `mcp_rpc.py`（别名） | `= McpErrorCode`（向后兼容） |
| `PUBLIC_MCP_ERROR_REASONS` | `error_codes.py` | — | 契约列表 tuple |

### 2.2 验证函数

| 函数名 | 定义位置 | 用途 |
|--------|---------|------|
| `verify_public_mcp_error_reasons()` | `error_codes.py` | 验证 PUBLIC_MCP_ERROR_REASONS 与 McpErrorReason 一致 |

---

## 3. 验证点清单

| 验证点 | 文件路径 | 验证内容 | 触发方式 |
|--------|---------|----------|----------|
| `TestErrorReasonWhitelistConsistency` | `tests/gateway/test_mcp_jsonrpc_contract.py` | ErrorReason ↔ Schema enum | `pytest` |
| `TestErrorDataSchemaValidation` | `tests/gateway/test_mcp_jsonrpc_contract.py` | error.data 结构 ↔ Schema | `pytest` |
| `check_mcp_jsonrpc_error_contract.py` | `scripts/ci/` | McpErrorReason ↔ Schema enum | `make check-mcp-error-contract` |
| `verify_public_mcp_error_reasons()` | `error_codes.py` | PUBLIC_MCP_ERROR_REASONS ↔ McpErrorReason | 编程调用 |

---

## 4. 当前不一致项与推荐决策

### 4.1 ⚠️ SSOT 声明不一致

**现状**：
- `docs/contracts/mcp_jsonrpc_error_v1.md` §13.3.1 声明：代码（`mcp_rpc.py:ErrorReason`）为 reason 常量权威来源
- `docs/contracts/mcp_jsonrpc_error_v1.md` §13.3.2 声明：Schema 为结构权威
- `tests/gateway/test_mcp_jsonrpc_contract.py` 第 50 行注释声明：Schema 为 SSOT
- `scripts/ci/check_mcp_jsonrpc_error_contract.py` 实际实现：Schema enum 与代码双向验证

**问题**：
多处文档/注释对 SSOT 的表述不完全一致，可能导致理解歧义。

**推荐决策**：统一为 "Schema enum 为 reason 枚举值的 SSOT，代码跟随同步"

**需要改动的文件**：
1. `docs/contracts/mcp_jsonrpc_error_v1.md` - 更新 §13.3.1 表述
2. 无需修改测试和 CI 脚本（已按此逻辑实现）

---

### 4.2 ✅ DEPENDENCY_MISSING 归属（已正确）

**现状**：
- `DEPENDENCY_MISSING` 仅在 `ToolResultErrorCode` 中定义
- 不在 Schema `error_reason` enum 中
- 不在 `McpErrorReason` 中
- 文档 §3.2 已明确说明

**状态**：已一致，无需改动

---

### 4.3 ⚠️ 豁免策略文档化不足

**现状**：
- `scripts/ci/check_mcp_jsonrpc_error_contract.py` 定义了 `SCHEMA_ONLY_EXEMPT` 和 `CODE_ONLY_EXEMPT` 豁免清单
- 当前两个清单均为空集
- 豁免策略和用途在契约文档中未明确说明

**推荐决策**：在契约文档中增加豁免策略说明

**需要改动的文件**：
1. `docs/contracts/mcp_jsonrpc_error_v1.md` - 新增豁免策略章节

---

### 4.4 ⚠️ -32000 (TOOL_EXECUTION_ERROR) 废弃状态

**现状**：
- 文档 §13.5 声明已废弃
- `McpErrorCode.TOOL_EXECUTION_ERROR` 仍保留在代码中（向后兼容）
- Schema `jsonrpc_error.code` enum 中不包含 -32000（正确）
- 代码中未删除常量定义

**状态**：按废弃周期保留，符合预期

**需要改动的文件**：无（当前状态正确）

---

## 5. 同步检查清单

当修改错误码相关内容时，按以下清单逐项检查：

### 5.1 新增 JSON-RPC error.data.reason 值

- [ ] `schemas/mcp_jsonrpc_error_v1.schema.json` - `definitions.error_reason.enum` 新增值
- [ ] `src/engram/gateway/error_codes.py` - `McpErrorReason` 新增常量
- [ ] `src/engram/gateway/error_codes.py` - `PUBLIC_MCP_ERROR_REASONS` 新增值
- [ ] `docs/contracts/mcp_jsonrpc_error_v1.md` - §4 对应分类表格新增行
- [ ] 运行验证：`make check-mcp-error-contract && pytest tests/gateway/test_mcp_jsonrpc_contract.py::TestErrorReasonWhitelistConsistency -q`

### 5.2 新增业务层 result.error_code 值

- [ ] `src/engram/gateway/result_error_codes.py` - `ToolResultErrorCode` 新增常量
- [ ] `docs/contracts/mcp_jsonrpc_error_v1.md` - §4.6 表格新增行
- [ ] **无需**更新 Schema enum（业务层错误码不属于 JSON-RPC 协议层）

### 5.3 废弃 reason 值

- [ ] 在代码常量上添加 `@deprecated` 注释
- [ ] 在 Schema 中保留（废弃周期内）
- [ ] 在文档中标注废弃状态
- [ ] 至少保留 2 个 MINOR 版本后再移除

---

## 6. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-02-01 | 初始版本，建立漂移矩阵 |
