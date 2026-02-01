# MCP JSON-RPC 2.0 错误模型契约 v1

> 版本: v1.0  
> 生效日期: 2026-01-30  
> 适用于: `src/engram/gateway/`

## 概述

本文档定义 MCP Gateway 的 JSON-RPC 2.0 错误处理契约，包括：
1. 错误返回策略（error response vs result）
2. 结构化 `error.data` 格式
3. 错误分类与原因码列表

## 1. 错误返回策略

### 1.1 JSON-RPC Error Response（协议层/系统层错误）

以下场景返回 JSON-RPC 标准错误响应（`error` 字段）：

| 场景 | 错误码 | 分类 |
|------|--------|------|
| JSON 解析失败 | -32700 (PARSE_ERROR) | protocol |
| 请求格式无效（缺少 method/jsonrpc） | -32600 (INVALID_REQUEST) | protocol |
| 方法不存在 | -32601 (METHOD_NOT_FOUND) | protocol |
| 参数校验失败（缺少必需参数、类型错误） | -32602 (INVALID_PARAMS) | validation |
| 未知工具（tools/call 中 name 不存在） | -32602 (INVALID_PARAMS) | validation |
| 工具执行器未注册 | -32603 (INTERNAL_ERROR) | internal |
| 未处理的运行时异常 | -32603 (INTERNAL_ERROR) | internal |
| 依赖服务不可用（如 OpenMemory 连接失败） | -32001 (DEPENDENCY_UNAVAILABLE) | dependency |
| 业务策略拒绝（如鉴权失败） | -32002 (BUSINESS_REJECTION) | business |

### 1.2 正常 Result（业务层响应）

以下场景返回正常的 `result` 响应（`ok=true/false` 在 result 内部）：

| 场景 | result 结构 | 说明 |
|------|-------------|------|
| memory_store 成功 | `{ok: true, ...}` | 业务成功 |
| memory_store 策略拒绝 | `{ok: false, action: "reject", ...}` | 业务层拒绝，可重试 |
| memory_store 重定向到 outbox | `{ok: true, action: "redirect", ...}` | 降级成功 |
| memory_query 查询成功 | `{ok: true, results: [...]}` | 业务成功 |
| memory_query 降级（OpenMemory 不可用） | `{ok: true, degraded: true, results: [...]}` | 降级到 Logbook 查询 |
| reliability_report 获取成功 | `{ok: true, ...}` | 只读操作成功 |
| governance_update 鉴权失败 | `{ok: false, action: "reject", ...}` | 业务层拒绝 |
| evidence_upload 成功 | `{ok: true, ...}` | 业务成功 |

**设计原则**：
- **协议/系统错误** → JSON-RPC error response（调用方应检查并处理）
- **业务决策** → 正常 result（ok=false 表示业务拒绝，但请求本身是有效的）
- **降级** → 正常 result（degraded=true 表示使用了降级路径）

## 2. 错误数据结构 (error.data)

所有 JSON-RPC 错误响应的 `error.data` 字段遵循以下结构：

```typescript
interface ErrorData {
  // 必需字段
  category: "protocol" | "validation" | "business" | "dependency" | "internal";
  reason: string;           // 错误原因码（见 §3）
  retryable: boolean;       // 是否建议重试
  
  // 可选字段
  correlation_id?: string;  // 追踪 ID（格式: corr-{16位十六进制}）
  details?: object;         // 附加详情（如 tool 名称、service 名称）
}
```

### 2.1 必需字段契约

**所有 JSON-RPC 错误响应必须包含：**

1. `error.data.category` - 错误分类
2. `error.data.reason` - 错误原因码
3. `error.data.retryable` - 是否可重试（布尔值）
4. `error.data.correlation_id` - 请求追踪 ID

### 2.2 示例

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32602,
    "message": "未知工具: nonexistent_tool",
    "data": {
      "category": "validation",
      "reason": "UNKNOWN_TOOL",
      "retryable": false,
      "correlation_id": "corr-a1b2c3d4e5f67890"
    }
  }
}
```

## 3. 错误分类与原因码

### 3.0 错误码命名空间边界

本章节定义了 Gateway 层两类错误码的边界与使用规范。

| 层级 | 字段位置 | 权威来源 | 命名规范 | 用途 | 同名策略 |
|------|----------|----------|----------|------|----------|
| MCP/JSON-RPC 层 | `error.data.reason` | `McpErrorReason` (`error_codes.py`) | `UPPER_SNAKE_CASE` | 协议层/系统层错误响应 | 禁止使用 `ToolResultErrorCode.*` |
| 业务结果层 | `result.error_code` | `ToolResultErrorCode` (`result_error_codes.py`) | `UPPER_SNAKE_CASE` | 工具执行业务层错误 | 禁止使用 `McpErrorReason.*` |

**边界规则**：
1. `error.data.reason` 只能使用 `McpErrorReason` 中定义的常量
2. `result.error_code` 只能使用 `ToolResultErrorCode` 中定义的常量
3. 两个命名空间相互隔离，即使名称相同（如 `MISSING_REQUIRED_PARAM`）也分属不同用途
4. 违反边界的代码将被 CI 门禁拒绝（参见 `tests/gateway/test_mcp_jsonrpc_contract.py::TestErrorCodeBoundaryMisuse`）

### 3.1 protocol - 协议层错误

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `PARSE_ERROR` | JSON 解析失败 | 否 |
| `INVALID_REQUEST` | JSON-RPC 请求格式无效 | 否 |
| `METHOD_NOT_FOUND` | 请求的 method 不存在 | 否 |

### 3.2 validation - 参数校验错误

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `MISSING_REQUIRED_PARAM` | 缺少必需参数 | 否 |
| `INVALID_PARAM_TYPE` | 参数类型错误 | 否 |
| `INVALID_PARAM_VALUE` | 参数值无效 | 否 |
| `UNKNOWN_TOOL` | tools/call 中指定的工具不存在 | 否 |

### 3.3 business - 业务拒绝

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `POLICY_REJECT` | 策略拒绝 | 取决于策略 |
| `AUTH_FAILED` | 鉴权失败 | 否 |
| `ACTOR_UNKNOWN` | 用户身份未知 | 否 |
| `GOVERNANCE_UPDATE_DENIED` | 治理更新被拒绝 | 否 |

### 3.4 dependency - 依赖服务错误

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `OPENMEMORY_UNAVAILABLE` | OpenMemory 服务不可用 | 是 |
| `OPENMEMORY_CONNECTION_FAILED` | OpenMemory 连接失败 | 是 |
| `OPENMEMORY_API_ERROR` | OpenMemory API 返回错误 | 5xx 可重试 |
| `LOGBOOK_DB_UNAVAILABLE` | Logbook 数据库不可用 | 是 |
| `LOGBOOK_DB_CHECK_FAILED` | Logbook 数据库检查失败 | 否 |

### 3.5 internal - 内部错误

| 原因码 | 说明 | 可重试 |
|--------|------|--------|
| `INTERNAL_ERROR` | 通用内部错误 | 否 |
| `TOOL_EXECUTOR_NOT_REGISTERED` | 工具执行器未注册 | 否 |
| `UNHANDLED_EXCEPTION` | 未处理的异常 | 否 |

## 4. JSON-RPC 错误码映射

| JSON-RPC 错误码 | 名称 | 对应分类 |
|-----------------|------|----------|
| -32700 | PARSE_ERROR | protocol |
| -32600 | INVALID_REQUEST | protocol |
| -32601 | METHOD_NOT_FOUND | protocol |
| -32602 | INVALID_PARAMS | validation |
| -32603 | INTERNAL_ERROR | internal |
| -32001 | DEPENDENCY_UNAVAILABLE | dependency |
| -32002 | BUSINESS_REJECTION | business |
| -32000 | TOOL_EXECUTION_ERROR | 已废弃，不再使用 |

## 5. tools/call 错误对齐

`tools/call` 方法的错误统一遵循以下规则：

### 5.1 参数错误 (缺少 name)

```json
{
  "error": {
    "code": -32602,
    "message": "缺少必需参数: name",
    "data": {
      "category": "validation",
      "reason": "MISSING_REQUIRED_PARAM",
      "retryable": false,
      "correlation_id": "corr-..."
    }
  }
}
```

### 5.2 未知工具 (name 不在可用工具列表)

```json
{
  "error": {
    "code": -32602,
    "message": "未知工具: xxx",
    "data": {
      "category": "validation",
      "reason": "UNKNOWN_TOOL",
      "retryable": false,
      "correlation_id": "corr-..."
    }
  }
}
```

**设计决策**：未知工具归类为 `validation` 而非 `protocol`，因为：
1. 方法 `tools/call` 存在，问题在于参数（工具名）无效
2. 与缺少参数错误保持一致的分类
3. 符合 JSON-RPC 2.0 规范（-32602 用于参数问题）

## 6. 实现参考

契约由以下模块实现：

- `src/engram/gateway/error_codes.py` - MCP/JSON-RPC 错误码定义
  - `McpErrorCode` - JSON-RPC 2.0 标准错误码
  - `McpErrorCategory` - 错误分类常量
  - `McpErrorReason` - 错误原因码常量
  - `to_jsonrpc_error()` - 统一异常转换函数

- `src/engram/gateway/result_error_codes.py` - 工具执行结果错误码
  - `ToolResultErrorCode` - 业务层 result.error_code 错误码

- `src/engram/gateway/mcp_rpc.py` - 错误模型与 JSON-RPC 处理
  - `ErrorData` - 结构化错误数据模型
  - `handle_tools_call()` - tools/call 处理器

### 推荐导入路径

**外部集成/插件开发者** 应从稳定公共 API 导入（所有错误码均为 **Tier A 核心稳定层**）：

```python
from engram.gateway.public_api import (
    # Tier A: 这些符号主版本内接口不变，可安全依赖
    McpErrorCode,       # JSON-RPC 错误码（如 -32602, -32001）
    McpErrorCategory,   # 错误分类（protocol/validation/business/dependency/internal）
    McpErrorReason,     # 错误原因码（如 UNKNOWN_TOOL, OPENMEMORY_UNAVAILABLE）
    ToolResultErrorCode,  # 业务层 result.error_code 错误码
)
```

> **Tier A 稳定性承诺**：上述错误码常量属于核心稳定层，主版本内不会变更常量值。
> 插件作者可安全依赖这些符号进行错误处理逻辑。
> 详见 [gateway_public_api_surface.md](../architecture/gateway_public_api_surface.md)

**内部模块** 可直接从定义模块导入：

```python
from engram.gateway.error_codes import McpErrorCode, McpErrorCategory, McpErrorReason
from engram.gateway.result_error_codes import ToolResultErrorCode
```

## 7. 测试覆盖

契约测试位于：

- `tests/gateway/test_mcp_jsonrpc_contract.py`
  - `TestErrorDataStructure` - ErrorData 结构验证
  - `TestErrorDataFields` - 必需字段完整性
  - `TestCorrelationIdTracking` - correlation_id 追踪

## 8. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-01-30 | 初始版本，固化错误模型契约 |
