# ADR: Gateway Public API JSON-RPC Surface 暴露决策

> 状态: **已批准**  
> 创建日期: 2026-02-02  
> 决策者: Engram Core Team

---

## 1. 背景与问题

### 1.1 当前状态

Gateway `public_api.py` 采用 Tier A/B/C 三层分类策略导出符号（参见 [gateway_public_api_surface.md](./gateway_public_api_surface.md)）：

- **Tier A**: 核心稳定层（直接导入，主版本内接口不变）
- **Tier B**: 可选依赖层（延迟导入，需外部依赖）
- **Tier C**: 便捷/内部层（可能在次版本调整签名）

JSON-RPC 相关符号目前分布在两个层级：

| 符号 | 当前 Tier | 导入方式 | 外部依赖 |
|------|-----------|----------|----------|
| `McpErrorCode` | A | 直接导入 | 无 |
| `McpErrorCategory` | A | 直接导入 | 无 |
| `McpErrorReason` | A | 直接导入 | 无 |
| `dispatch_jsonrpc_request` | B | 延迟导入 | pydantic |
| `JsonRpcDispatchResult` | B | 延迟导入 | pydantic |

### 1.2 核心问题

**是否应该暴露 JSON-RPC 请求分发入口（`dispatch_jsonrpc_request`）给外部插件/集成方？**

考量因素：
1. 暴露程度：完整 RPC 分发 vs 仅错误码常量
2. 兼容性承诺：签名/返回类型变更的影响范围
3. 错误语义：错误码/分类/原因码的稳定性
4. 测试覆盖：如何保证公开接口的契约稳定性

### 1.3 决策范围

本 ADR 针对以下符号做出暴露决策：

| 符号类型 | 符号名 | 决策内容 |
|----------|--------|----------|
| 常量类 | `McpErrorCode`, `McpErrorCategory`, `McpErrorReason` | 是否暴露、稳定性承诺 |
| 函数 | `dispatch_jsonrpc_request` | 是否暴露、签名契约 |
| 数据类 | `JsonRpcDispatchResult` | 是否暴露、字段契约 |

> **符号清单权威来源**：完整的导出符号清单以 `src/engram/gateway/public_api.py:__all__` 为准。
> 本 ADR 仅记录设计决策，不维护完整符号清单。详见 [gateway_public_api_surface.md](./gateway_public_api_surface.md)

---

## 2. 决策

### 2.1 暴露决策总览

| 符号 | 是否暴露 | Tier | 稳定性承诺 |
|------|----------|------|-----------|
| `McpErrorCode` | ✅ 是 | A | 主版本内常量值不变 |
| `McpErrorCategory` | ✅ 是 | A | 主版本内枚举值不变 |
| `McpErrorReason` | ✅ 是 | A | 主版本内常量值不变 |
| `dispatch_jsonrpc_request` | ✅ 是 | B | 主版本内签名不变 |
| `JsonRpcDispatchResult` | ✅ 是 | B | 主版本内字段结构不变 |

**决策理由**：

1. **错误码常量（Tier A）**：插件作者需要进行错误处理和分类，必须暴露
2. **JSON-RPC 分发入口（Tier B）**：允许高级集成场景（如自定义 HTTP 层），但标注为可选依赖层

### 2.2 暴露的符号规范

#### 2.2.1 McpErrorCode（Tier A - 常量类）

**暴露签名**：

```python
class McpErrorCode:
    """JSON-RPC 2.0 标准错误码"""
    PARSE_ERROR: int = -32700
    INVALID_REQUEST: int = -32600
    METHOD_NOT_FOUND: int = -32601
    INVALID_PARAMS: int = -32602
    INTERNAL_ERROR: int = -32603
    # 自定义错误码（-32099 ~ -32000）
    DEPENDENCY_ERROR: int = -32001
    BUSINESS_ERROR: int = -32002
```

**稳定性承诺**：
- 常量值（数字）主版本内不变
- 常量名主版本内不变
- 允许新增常量（向后兼容）

**错误语义**：
- `-32700` ~ `-32600`: JSON-RPC 2.0 标准协议错误
- `-32099` ~ `-32000`: 自定义服务器错误（Gateway 业务层）

#### 2.2.2 McpErrorCategory（Tier A - 常量类）

**暴露签名**：

```python
class McpErrorCategory:
    """错误分类枚举"""
    PROTOCOL: str = "protocol"      # 协议层错误
    VALIDATION: str = "validation"  # 参数校验错误
    BUSINESS: str = "business"      # 业务层错误
    DEPENDENCY: str = "dependency"  # 依赖服务错误
    INTERNAL: str = "internal"      # 内部错误
```

**稳定性承诺**：
- 枚举值主版本内不变
- 允许新增分类（向后兼容）
- 错误码与分类的映射关系主版本内不变

**与 McpErrorCode 的映射关系**：

| ErrorCode | ErrorCategory | 说明 |
|-----------|---------------|------|
| -32700 | protocol | JSON 解析失败 |
| -32600 | protocol | 请求格式无效 |
| -32601 | protocol | 方法不存在 |
| -32602 | validation | 参数校验失败 |
| -32603 | internal | 内部错误 |
| -32001 | dependency | 依赖服务错误 |
| -32002 | business | 业务规则错误 |

#### 2.2.3 McpErrorReason（Tier A - 常量类）

**暴露签名**：

```python
class McpErrorReason:
    """错误原因码常量"""
    # 协议层
    PARSE_ERROR: str = "PARSE_ERROR"
    INVALID_REQUEST: str = "INVALID_REQUEST"
    METHOD_NOT_FOUND: str = "METHOD_NOT_FOUND"
    
    # 校验层
    MISSING_REQUIRED_PARAM: str = "MISSING_REQUIRED_PARAM"
    INVALID_PARAM_TYPE: str = "INVALID_PARAM_TYPE"
    INVALID_PARAM_VALUE: str = "INVALID_PARAM_VALUE"
    UNKNOWN_TOOL: str = "UNKNOWN_TOOL"
    
    # 依赖层
    OPENMEMORY_UNAVAILABLE: str = "OPENMEMORY_UNAVAILABLE"
    OPENMEMORY_CONNECTION_FAILED: str = "OPENMEMORY_CONNECTION_FAILED"
    OPENMEMORY_API_ERROR: str = "OPENMEMORY_API_ERROR"
    LOGBOOK_DB_UNAVAILABLE: str = "LOGBOOK_DB_UNAVAILABLE"
    
    # 内部层
    INTERNAL_ERROR: str = "INTERNAL_ERROR"
    UNHANDLED_EXCEPTION: str = "UNHANDLED_EXCEPTION"
```

**稳定性承诺**：
- 常量值（字符串）主版本内不变
- 允许新增原因码（向后兼容）

**命名规范**：
- 协议/校验/依赖/内部层：大写 + 下划线（`PARSE_ERROR`）
- Outbox 层（内部使用）：小写 + 下划线（`outbox_flush_success`）

#### 2.2.4 dispatch_jsonrpc_request（Tier B - 函数）

**暴露签名**：

```python
async def dispatch_jsonrpc_request(
    body: Dict[str, Any],
    correlation_id: Optional[str] = None,
) -> JsonRpcDispatchResult:
    """
    分发 JSON-RPC 请求（便捷入口函数）
    
    此函数是 mcp_router.dispatch 的稳定包装，提供：
    1. 自动解析请求体
    2. 自动归一化 correlation_id
    3. 返回结构化结果（包含 response 和 correlation_id）
    
    Args:
        body: JSON-RPC 请求体 dict，应包含 jsonrpc, method, params, id 字段
        correlation_id: 关联 ID（可选）。若不提供或格式不合规，则自动生成。
    
    Returns:
        JsonRpcDispatchResult: 包含 response 和 correlation_id 的结果对象
    """
    ...
```

**稳定性承诺**：
- 函数签名主版本内不变
- 参数类型主版本内不变
- 返回类型主版本内不变
- 允许新增可选参数（向后兼容）

**错误语义**：
- 依赖缺失：由 `public_api.__getattr__` 在导入时抛出 `ImportError` + 安装指引
- 请求格式错误：返回 JSON-RPC 错误响应（不抛异常）
- 内部错误：返回 JSON-RPC 错误响应（不抛异常）

**注意**：此函数的参数 `body` 和 `correlation_id` **不是** keyword-only，这是为了保持简洁的调用风格。

#### 2.2.5 JsonRpcDispatchResult（Tier B - Pydantic 模型）

**暴露签名**：

```python
class JsonRpcDispatchResult(BaseModel):
    """
    JSON-RPC 请求分发结果（稳定 API）
    
    封装 dispatch 的结果，包含响应和 correlation_id。
    用于需要同时获取响应和 correlation_id 的场景。
    """
    response: JsonRpcResponse = Field(..., description="JSON-RPC 响应")
    correlation_id: str = Field(..., description="请求追踪 ID")
    
    @property
    def http_status(self) -> int:
        """
        根据 JSON-RPC 响应计算 HTTP 状态码
        
        映射规则：
        - 无错误 → 200 OK
        - PARSE_ERROR (-32700) → 400 Bad Request
        - INVALID_REQUEST (-32600) → 400 Bad Request
        - METHOD_NOT_FOUND (-32601) → 404 Not Found
        - INVALID_PARAMS (-32602) → 400 Bad Request
        - INTERNAL_ERROR (-32603) → 500 Internal Server Error
        - DEPENDENCY_UNAVAILABLE (-32001) → 503 Service Unavailable
        - BUSINESS_REJECTION (-32002) → 400 Bad Request
        - 其他错误 → 500 Internal Server Error
        """
        ...
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为 HTTP 响应体 dict
        
        返回 JsonRpcResponse 的字典表示，排除 None 值。
        """
        return self.response.model_dump(exclude_none=True)
```

**稳定性承诺**：
- 字段名（`response`, `correlation_id`）主版本内不变
- 字段类型主版本内不变
- `http_status` property 和 `to_dict()` 方法主版本内不变
- 允许新增可选字段（向后兼容）

**response 结构（成功）**：

`to_dict()` 返回值示例：

```json
{
    "jsonrpc": "2.0",
    "id": 1,
    "result": { ... }
}
```

**response 结构（错误）**：

`to_dict()` 返回值示例：

```json
{
    "jsonrpc": "2.0",
    "id": 1,
    "error": {
        "code": -32602,
        "message": "参数校验失败",
        "data": {
            "category": "validation",
            "reason": "MISSING_REQUIRED_PARAM",
            "retryable": false,
            "correlation_id": "corr-xxx"
        }
    }
}
```

**使用示例**：

```python
result = await dispatch_jsonrpc_request(body)
# 设置响应头
headers = {"X-Correlation-ID": result.correlation_id}
# 返回 HTTP 响应
return JSONResponse(
    content=result.to_dict(),
    status_code=result.http_status,
    headers=headers,
)
```

---

## 3. Tier 归类与兼容性承诺

> **完整兼容策略**：参见 [gateway_contract_convergence.md §11](../contracts/gateway_contract_convergence.md#11-public-api-向后兼容策略)
>
> 本章仅记录 JSON-RPC 相关符号的 Tier 归类决策依据，完整的向后兼容策略以上述契约文档为准。

### 3.1 Tier 归类原则

| Tier | 符号类型 | 归类原则 | JSON-RPC 相关示例 |
|------|----------|----------|-------------------|
| **A** | 常量/Protocol/数据类 | 无外部依赖，纯 Python | `McpErrorCode`, `McpErrorCategory`, `McpErrorReason` |
| **B** | 实现类/函数 | 需要外部依赖（pydantic 等） | `dispatch_jsonrpc_request`, `JsonRpcDispatchResult` |
| **C** | 便捷函数 | 可被 Tier A 替代 | （无 JSON-RPC 相关） |

### 3.1.1 Tier B 的"可选依赖"边界

**为何 `dispatch_jsonrpc_request` 和 `JsonRpcDispatchResult` 放在 Tier B？**

1. **外部依赖**：这两个符号定义在 `mcp_rpc.py` 中，该模块依赖 `pydantic`（用于 `BaseModel`）。虽然 `pydantic` 是 Gateway 的核心依赖，但为了保持 `public_api` 的导入轻量化，采用延迟导入策略。

2. **延迟导入收益**：
   - 首次 `import engram.gateway.public_api` 不会加载 `mcp_rpc.py`
   - 仅当实际使用这些符号时才触发模块加载
   - 减少 Tier A 符号用户的启动时间

**ImportError 语义由 `public_api.__getattr__` 提供**：

```python
# public_api.py 中的延迟导入机制
_TIER_B_LAZY_IMPORTS = {
    "dispatch_jsonrpc_request": (".mcp_rpc", "dispatch_jsonrpc_request"),
    "JsonRpcDispatchResult": (".mcp_rpc", "JsonRpcDispatchResult"),
}

def __getattr__(name: str) -> Any:
    if name in _TIER_B_LAZY_IMPORTS:
        module_path, attr_name = _TIER_B_LAZY_IMPORTS[name]
        try:
            module = importlib.import_module(module_path, __package__)
            return getattr(module, attr_name)
        except ImportError as e:
            # 格式化用户友好的错误消息
            raise ImportError(_format_import_error(...)) from e
```

当 `from engram.gateway.public_api import dispatch_jsonrpc_request` 执行时：
1. Python 调用 `public_api.__getattr__("dispatch_jsonrpc_request")`
2. 触发 `importlib.import_module(".mcp_rpc", ...)`
3. 若 `mcp_rpc.py` 的依赖（如 pydantic）不可用，抛出 `ImportError`
4. `__getattr__` 捕获并重新包装为用户友好的错误消息

**错误消息格式**（由 `_IMPORT_ERROR_TEMPLATE` 定义）：

```
ImportError: 无法导入 'dispatch_jsonrpc_request'（来自 .mcp_rpc）

原因: No module named 'pydantic'

此功能需要 MCP RPC 支持模块。
请安装：pip install -e ".[full]"
```

### 3.2 兼容性承诺矩阵

| 变更类型 | Tier A | Tier B | 需要废弃期 |
|----------|--------|--------|-----------|
| 新增常量/字段 | ✅ 允许 | ✅ 允许 | 否 |
| 新增可选参数 | ✅ 允许 | ✅ 允许 | 否 |
| 修改常量值 | ❌ 禁止 | - | - |
| 修改函数签名 | - | ❌ 禁止 | - |
| 修改字段类型 | ❌ 禁止 | ❌ 禁止 | - |
| 移除符号 | ❌ 禁止 | ❌ 禁止 | 2 个次版本 |
| 重命名符号 | ❌ 禁止 | ❌ 禁止 | 2 个次版本 |

### 3.3 语义不变量

| 编号 | 不变量 | 测试锚点 |
|------|--------|----------|
| JSONRPC-API-01 | `McpErrorCode` 常量值主版本内不变 | `tests/gateway/test_error_codes.py::TestErrorCodeConstants` |
| JSONRPC-API-02 | `McpErrorCategory` 与 `McpErrorCode` 映射关系主版本内不变 | `tests/gateway/test_error_codes.py::TestErrorCodeConsistency` |
| JSONRPC-API-03 | `McpErrorReason` 常量值主版本内不变 | `tests/gateway/test_mcp_jsonrpc_contract.py::TestErrorReasonWhitelistConsistency` |
| JSONRPC-API-04 | `dispatch_jsonrpc_request` 返回 `JsonRpcDispatchResult` 类型 | `tests/gateway/test_public_api_exports.py::TestTierBExports` |
| JSONRPC-API-05 | `JsonRpcDispatchResult.response` 通过 `to_dict()` 符合 JSON-RPC 2.0 格式 | `tests/gateway/test_mcp_jsonrpc_contract.py::TestJsonRpcInvalidRequest` |
| JSONRPC-API-06 | `JsonRpcDispatchResult` 是 Pydantic 模型，有 `response` 和 `correlation_id` 字段 | `tests/gateway/test_public_api_import_contract.py::TestPublicApiMcpRpcTierBImport::test_jsonrpc_dispatch_result_importable` |
| JSONRPC-API-07 | `JsonRpcDispatchResult` 有 `to_dict()` 方法和 `http_status` property | `tests/gateway/test_public_api_import_contract.py::TestPublicApiMcpRpcTierBImport::test_jsonrpc_dispatch_result_has_to_dict_and_http_status` |
| JSONRPC-API-08 | `dispatch_jsonrpc_request` 返回的结果支持 `http_status` 和 `to_dict()` | `tests/gateway/test_public_api_import_contract.py::TestPublicApiMcpRpcTierBImport::test_dispatch_jsonrpc_request_returns_result_with_http_methods` |
| JSONRPC-API-09 | `dispatch_jsonrpc_request` 是异步函数 | `tests/gateway/test_public_api_import_contract.py::TestPublicApiMcpRpcTierBImport::test_dispatch_jsonrpc_request_importable` |

---

## 4. CI Gates 与测试矩阵要求

### 4.1 必需 CI Gates

| Gate | 检查内容 | 触发条件 |
|------|----------|----------|
| `check-gateway-public-api-surface` | Tier B 符号延迟导入策略 | `public_api.py` 变更 |
| `test-public-api-exports` | Tier A/B 符号导出完整性 | `public_api.py` 变更 |
| `test-error-codes` | 错误码常量值稳定性 | `error_codes.py` 变更 |
| `test-mcp-jsonrpc-contract` | JSON-RPC 响应格式契约 | `mcp_rpc.py` 变更 |

### 4.2 测试矩阵

#### 4.2.1 Tier A 符号测试

| 测试场景 | 测试文件 | 说明 |
|----------|----------|------|
| 错误码常量值 | `test_error_codes.py::TestErrorCodeConstants` | 验证常量数值不变 |
| 错误码与分类映射 | `test_error_codes.py::TestErrorCodeConsistency` | 验证映射关系 |
| 原因码白名单 | `test_mcp_jsonrpc_contract.py::TestErrorReasonWhitelistConsistency` | 验证公开常量 |
| 导入无外部依赖 | `test_public_api_import_contract.py::test_tier_a_no_external_deps` | 验证纯 Python |

#### 4.2.2 Tier B 符号测试

| 测试场景 | 测试文件 | 说明 |
|----------|----------|------|
| 延迟导入策略 | `test_public_api_import_contract.py::TestPublicApiModuleLevelImportSafe::test_module_import_succeeds_without_logbook_adapter` | 验证 `__getattr__` 延迟导入机制 |
| 依赖缺失时 ImportError | `test_public_api_import_contract.py::TestPublicApiImportErrorMessageQuality` | 验证错误消息包含模块名、安装指引 |
| `dispatch_jsonrpc_request` 可导入 | `test_public_api_import_contract.py::TestPublicApiMcpRpcTierBImport::test_dispatch_jsonrpc_request_importable` | 验证是异步函数 |
| `JsonRpcDispatchResult` 字段契约 | `test_public_api_import_contract.py::TestPublicApiMcpRpcTierBImport::test_jsonrpc_dispatch_result_importable` | 验证 `response` 和 `correlation_id` 字段 |
| `JsonRpcDispatchResult` 方法契约 | `test_public_api_import_contract.py::TestPublicApiMcpRpcTierBImport::test_jsonrpc_dispatch_result_has_to_dict_and_http_status` | 验证 `to_dict()` 和 `http_status` |
| 返回类型契约 | `test_public_api_import_contract.py::TestPublicApiMcpRpcTierBImport::test_dispatch_jsonrpc_request_returns_result_with_http_methods` | 验证返回结果支持 HTTP 方法 |
| mcp_rpc 符号不影响 Tier A | `test_public_api_import_contract.py::TestPublicApiMcpRpcTierBImport::test_mcp_rpc_symbols_do_not_affect_tier_a` | 验证导入隔离 |
| 函数签名稳定性 | `test_public_api_exports.py::TestProtocolSignatures` | 验证签名不变 |
| 返回类型契约 | `test_public_api_exports.py::test_dispatch_return_type` | 验证返回 `JsonRpcDispatchResult` |

#### 4.2.3 JSON-RPC 响应格式测试

| 测试场景 | 测试文件 | 说明 |
|----------|----------|------|
| 成功响应格式 | `test_mcp_jsonrpc_contract.py::TestToolsList` | 验证 result 字段 |
| 错误响应格式 | `test_mcp_jsonrpc_contract.py::TestErrorDataStructure` | 验证 error.data 结构 |
| correlation_id 传递 | `test_mcp_jsonrpc_contract.py::TestCorrelationIdSingleSourceContract` | 验证追踪 ID 一致性 |

### 4.3 最小验收命令集

```bash
# 1. public_api.py Tier B 延迟导入策略检查
python scripts/ci/check_gateway_public_api_import_surface.py

# 2. 错误码常量稳定性测试
pytest tests/gateway/test_error_codes.py::TestErrorCodeConstants -v

# 3. JSON-RPC 响应格式契约测试
pytest tests/gateway/test_mcp_jsonrpc_contract.py -v

# 4. Public API 导出完整性测试
pytest tests/gateway/test_public_api_exports.py -v

# 5. Tier A/B 分层导入契约测试
pytest tests/gateway/test_public_api_import_contract.py -v
```

**单行执行**：

```bash
python scripts/ci/check_gateway_public_api_import_surface.py && \
pytest tests/gateway/test_error_codes.py::TestErrorCodeConstants \
       tests/gateway/test_mcp_jsonrpc_contract.py \
       tests/gateway/test_public_api_exports.py \
       tests/gateway/test_public_api_import_contract.py -v
```

---

## 5. 迁移/废弃策略

### 5.1 废弃流程

当需要废弃 JSON-RPC 相关公开符号时，必须遵循以下流程：

```
Phase 1: 标记废弃（次版本 N）
├── 在代码中添加 warnings.warn(DeprecationWarning)
├── 在文档中标记 [DEPRECATED]
└── 提供替代方案说明

Phase 2: 废弃期（次版本 N ~ N+1）
├── 保持符号可用
├── 日志/监控记录废弃符号使用频率
└── 通知已知使用方

Phase 3: 移除（主版本 M+1）
├── 从 __all__ 移除
├── 从代码移除（或保留内部使用）
└── 更新文档
```

### 5.2 从"文档承诺"回退策略

若因技术原因需要回退已发布的兼容性承诺：

| 回退类型 | 处理方式 | 最小废弃期 |
|----------|----------|-----------|
| 移除 Tier A 符号 | 禁止（破坏性变更） | - |
| 移除 Tier B 符号 | 发布安全公告 + 废弃期 | 2 个次版本 |
| 修改常量值 | 禁止（破坏性变更） | - |
| 修改函数签名 | 引入新函数 + 废弃旧函数 | 2 个次版本 |
| 修改返回类型结构 | 新增字段（向后兼容） | 0（允许） |

### 5.3 版本升级检查清单

当准备主版本升级（如 v1.x → v2.0）时：

- [ ] 审查所有标记为 `[DEPRECATED]` 的符号
- [ ] 确认废弃期已满（至少 2 个次版本）
- [ ] 更新 CHANGELOG 记录移除的符号
- [ ] 更新 `gateway_public_api_surface.md`
- [ ] 更新 `gateway_contract_convergence.md §11`
- [ ] 运行完整测试矩阵

### 5.4 紧急回退（安全漏洞/严重 Bug）

若因安全漏洞或严重 Bug 需要紧急修改公开 API：

1. **立即发布安全公告**
2. **发布修复版本**（可跳过废弃期）
3. **在 CHANGELOG 明确标注破坏性变更**
4. **提供迁移指南**

---

## 6. 插件作者指南

### 6.1 推荐导入方式

**错误处理（推荐）**：

```python
from engram.gateway.public_api import (
    McpErrorCode,
    McpErrorCategory,
    McpErrorReason,
)

# 处理 JSON-RPC 错误响应
def handle_error(response: dict) -> None:
    error = response.get("error", {})
    code = error.get("code")
    data = error.get("data", {})
    
    if code == McpErrorCode.INVALID_PARAMS:
        reason = data.get("reason")
        if reason == McpErrorReason.UNKNOWN_TOOL:
            # 工具不存在，不可重试
            raise ToolNotFoundError(...)
        elif reason == McpErrorReason.MISSING_REQUIRED_PARAM:
            # 参数缺失，不可重试
            raise MissingParamError(...)
    
    elif code == McpErrorCode.DEPENDENCY_ERROR:
        # 依赖服务错误，可重试
        if data.get("retryable", False):
            raise RetryableError(...)
```

**自定义 HTTP 层集成（高级）**：

```python
try:
    from engram.gateway.public_api import (
        dispatch_jsonrpc_request,
        JsonRpcDispatchResult,
    )
    JSONRPC_AVAILABLE = True
except ImportError:
    JSONRPC_AVAILABLE = False

# 在自定义 HTTP 框架中使用
async def custom_mcp_endpoint(request):
    if not JSONRPC_AVAILABLE:
        return {"error": "JSON-RPC 支持不可用"}
    
    body = await request.json()
    result: JsonRpcDispatchResult = await dispatch_jsonrpc_request(
        body,
        correlation_id=request.headers.get("X-Correlation-ID"),
    )
    
    # 使用 to_dict() 获取响应体，http_status 获取 HTTP 状态码
    return Response(
        content=json.dumps(result.to_dict()),
        status_code=result.http_status,
        headers={"X-Correlation-ID": result.correlation_id},
    )
```

### 6.2 不推荐做法

```python
# ❌ 不要硬编码错误码数值
if error["code"] == -32602:  # 应使用 McpErrorCode.INVALID_PARAMS
    ...

# ❌ 不要硬编码原因码字符串
if data["reason"] == "UNKNOWN_TOOL":  # 应使用 McpErrorReason.UNKNOWN_TOOL
    ...

# ❌ 不要直接导入内部模块
from engram.gateway.mcp_rpc import ErrorReason  # 应从 public_api 导入
```

---

## 7. 相关文档

| 文档 | 路径 | 关联章节 |
|------|------|----------|
| Public API Surface 导出项分析 | [gateway_public_api_surface.md](./gateway_public_api_surface.md) | 完整导出清单 |
| Gateway 契约收敛文档 | [gateway_contract_convergence.md](../contracts/gateway_contract_convergence.md) | §1 MCP JSON-RPC 域, §11 Public API 向后兼容策略 |
| MCP JSON-RPC 错误模型契约 | [mcp_jsonrpc_error_v2.md](../contracts/mcp_jsonrpc_error_v2.md) | 错误响应结构 |
| Gateway ImportError 规范 | [gateway_importerror_and_optional_deps.md](./gateway_importerror_and_optional_deps.md) | Tier B 失败语义 |

---

## 8. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-02-02 | 初始版本，明确 JSON-RPC 相关符号暴露决策 |
| v1.1 | 2026-02-02 | 更新 `dispatch_jsonrpc_request` 签名（参数名 `body`，非 keyword-only）；更新 `JsonRpcDispatchResult` 为 Pydantic 模型（字段 `response`, `correlation_id`，property `http_status`，方法 `to_dict()`）；明确 Tier B ImportError 语义由 `public_api.__getattr__` 提供；补充与 `test_public_api_import_contract.py` 测试锚点对应关系 |
| v1.2 | 2026-02-02 | 明确符号清单权威来源层级：ADR 仅记录决策，完整符号清单以 `public_api.__all__` 为准；兼容承诺统一指向 `gateway_contract_convergence.md §11` |
