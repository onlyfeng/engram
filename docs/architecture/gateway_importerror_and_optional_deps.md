# Gateway ImportError 与可选依赖处理规范

> 创建日期: 2026-02-01
>
> 状态: Active

## 概述

本文档汇总 `src/engram/gateway/**/*.py` 中的 ImportError 捕获与 re-raise 模式，
并为每个模块标注导入时机、依赖可选性、缺失时期望行为，以及统一的处理准则。

## 导入时机定义

| 时机 | 触发条件 | 示例 |
|------|---------|------|
| **import-time** | 模块被 `import` 时立即执行 | `from .logbook_adapter import get_adapter` |
| **create_app-time** | `create_app()` 函数执行时 | app 工厂中的条件导入 |
| **lifespan-time** | FastAPI lifespan 上下文启动时 | `get_config()`, `get_container()` |
| **request-time** | HTTP 请求处理时（函数内延迟导入） | handler 内部的 `try: from ... import ...` |

---

## 模块 ImportError 汇总表

### 入口层模块

| 模块 | 位置（行号） | 导入时机 | 依赖模块 | 可选性 | 缺失行为 |
|------|-------------|---------|---------|--------|---------|
| `main.py` | L95-117 | **CLI 启动时** | `engram.logbook.errors` | **必需** | 打印详细错误 + `sys.exit(1)` |
| `lifecycle.py` | L36-44 | import-time | `config`, `container`, `startup` | **必需** | import-time 失败 |
| `lifecycle.py` | L104-142 | **lifespan-time** | `get_config()`, `get_container()` | 可选 | 警告但不阻止启动（优雅降级） |
| `routes.py` | L98-122 | **register_routes-time** | `dependencies`, `handlers`, `mcp_rpc`, `minio_audit_webhook`, `entrypoints.tool_executor` | **必需** | 注册失败 |
| `middleware.py` | L94-95 | **request-time** | `mcp_rpc.generate_correlation_id` | **必需** | 延迟导入，请求时才触发 |

### 核心适配层模块

| 模块 | 位置（行号） | 导入时机 | 依赖模块 | 可选性 | 缺失行为 |
|------|-------------|---------|---------|--------|---------|
| `logbook_adapter.py` | L81-98 | import-time | `engram.logbook` 核心模块 | **必需** | `raise ImportError`（含安装指引） |
| `logbook_adapter.py` | L116-124 | import-time | `engram.logbook.migrate` | 可选 | `_DB_MIGRATE_AVAILABLE=False` |
| `evidence_store.py` | L29-40 | import-time | `engram.logbook` 子模块 | **必需** | `raise ImportError`（含安装指引） |

### 协议/服务层模块

| 模块 | 位置（行号） | 导入时机 | 依赖模块 | 可选性 | 缺失行为 |
|------|-------------|---------|---------|--------|---------|
| `mcp_rpc.py` | L63-72 | import-time | `openmemory_client` 异常类 | 可选 | 设置为 `None`，降级跳过类型检查 |
| `mcp_rpc.py` | L76-84 | import-time | `logbook_adapter.LogbookDBCheckError` | 可选 | 设置为 `None` |
| `services/actor_validation.py` | L25-33 | import-time | `engram.logbook.errors.ErrorCode` | 可选 | 定义 Fallback 类 |
| `services/audit_service.py` | - | import-time | `audit_event`, `logbook_adapter` | **必需** | import-time 失败 |

### Handler 层模块

| 模块 | 位置（行号） | 导入时机 | 依赖模块 | 可选性 | 缺失行为 |
|------|-------------|---------|---------|--------|---------|
| `handlers/evidence_upload.py` | L72-99 | **request-time** | `evidence_store` | 可选 | 返回结构化错误响应 |
| `handlers/memory_store.py` | - | import-time | `di`, `mcp_rpc`, `policy` | **必需** | import-time 失败 |
| `handlers/memory_query.py` | - | import-time | `di`, `mcp_rpc` | **必需** | import-time 失败 |
| `handlers/governance_update.py` | - | import-time | `di`, `mcp_rpc` | **必需** | import-time 失败 |

### Worker 模块

| 模块 | 位置（行号） | 导入时机 | 依赖模块 | 可选性 | 缺失行为 |
|------|-------------|---------|---------|--------|---------|
| `reconcile_outbox.py` | L68-84 | import-time | `engram.logbook.errors.ErrorCode` | **必需** | 打印错误 + `sys.exit(1)` |
| `outbox_worker.py` | L34-52 | import-time | `engram.logbook.errors.ErrorCode` | **必需** | 打印错误 + `sys.exit(1)` |

---

## 详细分析

### 1. main.py（Gateway 入口）

```python
# L95-117: main() 函数内部 (CLI 启动时检查)
def main():
    # ... CLI 参数解析 ...
    # 检查 engram.logbook 模块是否可用（仅 CLI 启动时检查）
    try:
        import engram.logbook.errors  # noqa: F401
    except ImportError:
        print("..." + 详细安装指引)
        sys.exit(1)
```

- **导入时机**: **CLI 启动时**（`main()` 函数内部），非 import-time
- **依赖**: `engram.logbook.errors`
- **可选性**: **必需** — Gateway 核心功能依赖 engram_logbook 包
- **缺失行为**: 打印用户友好的错误消息（含 pip 安装命令），然后 `sys.exit(1)`
- **设计理由**: 
  - `from engram.gateway.main import app` 不触发依赖检查（支持 uvicorn 加载）
  - `python -m engram.gateway.main` 时才在 `main()` 中检查依赖
  - 这确保模块导入时不依赖 engram_logbook，同时 CLI 启动时尽早检测依赖缺失

### 1.5. lifecycle.py（生命周期管理）

```python
# L36-44: import-time 依赖（必需）
from .config import ConfigError, get_config, validate_config
from .container import (
    GatewayContainer, get_container, is_container_set,
    reset_container, set_container,
)
from .startup import check_logbook_db_on_startup

# L104-142: lifespan-time 依赖（可选，优雅降级）
try:
    config = get_config()
    validate_config()
    # ... 初始化逻辑
except ConfigError as e:
    logger.warning(f"配置加载失败: {e}（测试环境可忽略）")
except Exception as e:
    logger.warning(f"增强初始化异常: {e}（服务将继续启动）")
```

- **导入时机**: 
  - import-time: `config`, `container`, `startup` 模块
  - lifespan-time: `get_config()`, `get_container()` 调用
- **依赖**:
  - import-time: Gateway 内部模块（必需）
  - lifespan-time: 配置验证、DB 连接、依赖预热（可选）
- **可选性**: 
  - import-time 依赖是必需的
  - lifespan-time 的增强初始化是可选的
- **缺失行为**: 
  - import-time 依赖缺失: 模块导入失败
  - lifespan-time 配置缺失: 警告但不阻止启动，服务继续运行（优雅降级）
- **设计理由**: 
  - 支持测试环境（可能没有完整配置）正常工作
  - 生产环境通过 main() 的预检查保证配置完整性
  - lifespan 优雅降级确保即使配置不完整，服务仍能启动并响应 /health

### 1.6. routes.py（路由注册模块）

```python
# L98-122: register_routes-time 延迟导入
def register_routes(app: FastAPI) -> None:
    # 延迟导入: 在函数调用时才导入，不在模块顶层导入
    from .dependencies import get_deps_for_request, get_request_correlation_id_or_new
    from .handlers import (
        GovernanceSettingsUpdateResponse, MemoryQueryResponse, MemoryStoreResponse,
        governance_update_impl, memory_query_impl, memory_store_impl,
    )
    from .mcp_rpc import (
        ErrorCategory, ErrorData, ErrorReason, JsonRpcErrorCode,
        is_jsonrpc_request, make_jsonrpc_error, mcp_router,
        parse_jsonrpc_request, register_tool_executor,
    )
    from .minio_audit_webhook import router as minio_audit_router
    from .entrypoints.tool_executor import execute_tool as _execute_tool_impl
```

- **导入时机**: register_routes-time（`create_app()` 调用时）
- **依赖**: `dependencies`, `handlers`, `mcp_rpc`, `minio_audit_webhook`, `entrypoints.tool_executor`
- **可选性**: **必需** — 路由注册是 Gateway 核心功能
- **缺失行为**: 路由注册失败，应用无法启动
- **设计理由**:
  - 延迟导入确保模块级别 `import routes` 不触发 `get_config()`
  - 支持 `create_app()` 在无环境变量时被调用（测试场景）

### 1.7. middleware.py（中间件模块）

```python
# L94-95: request-time 延迟导入
class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 延迟导入：避免 import-time 依赖
        from .mcp_rpc import generate_correlation_id
        correlation_id = generate_correlation_id()
        ...
```

- **导入时机**: request-time（每次请求处理时）
- **依赖**: `mcp_rpc.generate_correlation_id`
- **可选性**: **必需** — correlation_id 是请求追踪的核心
- **缺失行为**: 延迟导入失败时，请求处理异常
- **设计理由**:
  - 中间件安装时（`install_middleware()`）不触发 `mcp_rpc` 模块的完整加载
  - 支持 import-safe 的模块结构

### 2. mcp_rpc.py（MCP JSON-RPC 协议层）

```python
# L63-72: OpenMemory 异常类
try:
    from engram.gateway.openmemory_client import (
        OpenMemoryAPIError,
        OpenMemoryConnectionError,
        OpenMemoryError,
    )
except ImportError:
    OpenMemoryError = None  # type: ignore
    OpenMemoryConnectionError = None
    OpenMemoryAPIError = None

# L76-84: LogbookDBCheckError
_LogbookDBCheckError: Optional[type] = None
try:
    from engram.gateway.logbook_adapter import LogbookDBCheckError as _ImportedLogbookDBCheckError
    if isinstance(_ImportedLogbookDBCheckError, type):
        _LogbookDBCheckError = _ImportedLogbookDBCheckError
except ImportError:
    pass
```

- **导入时机**: import-time
- **依赖**: `openmemory_client` 异常类、`logbook_adapter.LogbookDBCheckError`
- **可选性**: 可选 — 仅用于 `to_jsonrpc_error()` 中的 `isinstance` 类型检查
- **缺失行为**: 设置为 `None`，后续通过 `if XxxError is not None and isinstance(...)` 安全跳过
- **设计理由**: 协议层不应因类型导入失败而崩溃；错误分类降级为通用处理

### 3. logbook_adapter.py（Logbook 适配器）

```python
# L81-98: 核心依赖
try:
    from engram.logbook import governance, outbox
    from engram.logbook.config import Config
    from engram.logbook.db import get_connection, ...
    from engram.logbook.errors import DatabaseError
except ImportError as e:
    raise ImportError(
        f'logbook_adapter 需要 engram_logbook 模块: {e}\n'
        '请先安装:\n  pip install -e ".[full]"'
    )

# L116-124: 迁移模块（可选）
_DB_MIGRATE_AVAILABLE = False
try:
    from engram.logbook.migrate import run_all_checks, run_migrate
    _DB_MIGRATE_AVAILABLE = True
except ImportError:
    pass
```

- **导入时机**: import-time
- **依赖**: 
  - `engram.logbook.*` 核心模块（**必需**）
  - `engram.logbook.migrate`（可选）
- **可选性**: 核心依赖必需，迁移模块可选
- **缺失行为**: 
  - 核心依赖缺失: `raise ImportError` + 安装指引
  - 迁移模块缺失: `_DB_MIGRATE_AVAILABLE=False`，`check_db_schema()` 返回降级结果
- **设计理由**: 适配器是 Gateway ↔ Logbook 的桥梁，核心功能不可降级

### 4. logbook_db.py（已移除）

> **注意**: 此模块已在 v1.0 中完全移除。原有功能已合并到 `logbook_adapter.py`。
> 详见: `docs/architecture/deprecated_logbook_db_references_ssot.md`

### 5. evidence_store.py（证据存储模块）

```python
# L29-40
try:
    from engram.logbook.artifact_store import (
        ArtifactError, ArtifactSizeLimitExceededError, get_artifact_store,
    )
    from engram.logbook.db import attach as db_attach
    from engram.logbook.uri import build_attachment_evidence_uri
except ImportError as e:
    raise ImportError(
        f'evidence_store 需要 engram_logbook 模块: {e}\n'
        '请先安装:\n  pip install -e ".[full]"'
    )
```

- **导入时机**: import-time
- **依赖**: `engram.logbook.artifact_store`, `engram.logbook.db`, `engram.logbook.uri`
- **可选性**: **必需**
- **缺失行为**: `raise ImportError` + 安装指引
- **设计理由**: 证据存储功能强依赖 Logbook 存储后端，无法降级

### 6. handlers/evidence_upload.py（evidence_upload 工具处理器）

```python
# L72-99: 函数内延迟导入，使用统一 helper 函数
from ..mcp_rpc import make_dependency_missing_error

async def execute_evidence_upload(...) -> Dict[str, Any]:
    try:
        from ..evidence_store import (
            ALLOWED_CONTENT_TYPES, EvidenceContentTypeError, ...
        )
    except ImportError as import_err:
        logger.warning(f"evidence_store 导入失败: {import_err}")
        return make_dependency_missing_error(
            message="evidence_upload 功能依赖 engram_logbook 模块...",
            suggestion="请确保 engram_logbook 模块已正确安装...",
            missing_module="engram_logbook",
            import_error=str(import_err),
        )
```

- **导入时机**: **request-time**（函数内延迟导入）
- **依赖**: `evidence_store`
- **可选性**: 可选
- **缺失行为**: 返回结构化错误响应 `DEPENDENCY_MISSING`（使用 `make_dependency_missing_error` 统一构造）
- **设计理由**: 
  - **最佳实践示例** — handler 不在 import-time 触发外部依赖
  - 允许 Gateway 启动，即使 evidence_upload 功能不可用
  - 使用统一 helper 函数确保错误格式一致性
  - 返回结构化错误，便于客户端处理

### 7. services/actor_validation.py

```python
# L25-33
try:
    from engram.logbook.errors import ErrorCode
except ImportError:
    class ErrorCode:  # Fallback for testing
        ACTOR_UNKNOWN_REJECT = "actor_unknown:reject"
        ACTOR_UNKNOWN_DEGRADE = "actor_unknown:degrade"
        # ...
```

- **导入时机**: import-time
- **依赖**: `engram.logbook.errors.ErrorCode`
- **可选性**: 可选
- **缺失行为**: 定义 Fallback 类（硬编码错误码）
- **设计理由**: 支持独立测试场景，不强依赖完整 engram_logbook

### 8. reconcile_outbox.py / outbox_worker.py（后台 Worker）

```python
# reconcile_outbox.py L68-84 / outbox_worker.py L34-52
try:
    from engram.logbook.errors import ErrorCode
except ImportError:
    print("..." + 详细安装指引)
    sys.exit(1)
```

- **导入时机**: import-time
- **依赖**: `engram.logbook.errors.ErrorCode`
- **可选性**: **必需**
- **缺失行为**: 打印错误消息 + `sys.exit(1)`
- **设计理由**: Worker 是独立进程，核心依赖缺失时应立即终止

---

## 统一处理准则

### 原则 1: Handlers 不得在 import-time 触发外部依赖导入

```python
# ❌ 不推荐: handler 模块顶层导入
from ..evidence_store import upload_evidence  # import-time 触发

# ✅ 推荐: 函数内延迟导入
async def execute_evidence_upload(...):
    try:
        from ..evidence_store import upload_evidence
    except ImportError:
        return {"ok": False, "error_code": "DEPENDENCY_MISSING", ...}
```

**理由**:
- 允许 `create_app()` 在缺少可选依赖时仍能成功
- 隔离故障影响范围：单个功能不可用不影响整体启动
- 便于测试：可以 mock 延迟导入的模块

### 原则 2: 必需依赖在入口层快速失败

```python
# main.py / outbox_worker.py 等入口
try:
    import engram.logbook.errors
except ImportError:
    print("详细错误消息和安装指引...")
    sys.exit(1)
```

**理由**:
- 提供用户友好的错误消息，而非堆栈跟踪
- 避免运行时延迟失败导致的状态不一致

### 原则 3: 可选依赖设置降级标志或 None

```python
# 模式 A: 布尔标志
_FEATURE_AVAILABLE = False
try:
    from some_optional import feature
    _FEATURE_AVAILABLE = True
except ImportError:
    pass

# 使用时检查
if _FEATURE_AVAILABLE:
    feature()
else:
    fallback()

# 模式 B: 设置为 None
SomeException = None
try:
    from some_optional import SomeException
except ImportError:
    pass

# 使用时检查
if SomeException is not None and isinstance(e, SomeException):
    handle_specific_error()
```

### 原则 4: 结构化错误响应优于抛出异常

使用 `mcp_rpc.make_dependency_missing_error()` 统一构造依赖缺失错误：

```python
# ❌ 不推荐: 在 handler 中抛出异常到 app 层
async def my_handler(...):
    try:
        from ..some_module import func
    except ImportError as e:
        raise RuntimeError(f"依赖缺失: {e}")  # 会变成 500 错误

# ❌ 不推荐: 手动构造错误字典
async def my_handler(...):
    try:
        from ..some_module import func
    except ImportError:
        return {
            "ok": False,
            "error_code": "DEPENDENCY_MISSING",
            "retryable": False,
            "message": "功能依赖 xxx 模块",
            "suggestion": "安装命令...",
        }

# ✅ 推荐: 使用统一 helper 函数
from ..mcp_rpc import make_dependency_missing_error

async def my_handler(...):
    try:
        from ..some_module import func
    except ImportError as import_err:
        return make_dependency_missing_error(
            message="功能依赖 xxx 模块，当前未安装或配置不正确",
            suggestion='请确保 xxx 模块已正确安装：pip install -e ".[full]"',
            missing_module="xxx_module",
            import_error=str(import_err),
        )
```

**`make_dependency_missing_error` 契约字段:**
- `ok`: `False`（必需）
- `error_code`: `"DEPENDENCY_MISSING"`（必需）
- `retryable`: `False`（必需，依赖缺失不可重试）
- `message`: 错误描述（必需）
- `suggestion`: 解决建议（必需）
- `details`: 附加信息（必需，包含 `missing_module`）

### 原则 5: 使用 type: ignore 标注可选导入

```python
try:
    from some_module import SomeClass
except ImportError:
    SomeClass = None  # type: ignore[misc, assignment]
```

**理由**: 避免 mypy 类型检查错误，同时保持代码可读性

### 原则 6: 包 `__init__.py` 不得 eager-import 子模块

```python
# ❌ 禁止: __init__.py 顶层导入子模块
# src/engram/gateway/__init__.py
from . import logbook_adapter  # import engram.gateway 时触发整个依赖链
from . import openmemory_client
from .main import app  # 触发 create_app()

# ✅ 正确: 懒加载策略（当前实现）
# src/engram/gateway/__init__.py
from typing import TYPE_CHECKING

__all__ = ["__version__", "logbook_adapter", "openmemory_client", "outbox_worker"]

if TYPE_CHECKING:
    from . import logbook_adapter as logbook_adapter
    from . import openmemory_client as openmemory_client
    from . import outbox_worker as outbox_worker

_LAZY_SUBMODULES = {"logbook_adapter", "openmemory_client", "outbox_worker"}

def __getattr__(name: str):
    """懒加载子模块"""
    if name in _LAZY_SUBMODULES:
        import importlib
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

**理由**:
- `import engram.gateway` 不应触发 `logbook_adapter`、`openmemory_client` 等依赖链
- 允许静态类型检查（`TYPE_CHECKING` 块）同时保持运行时轻量
- 支持选择性导入：`from engram.gateway import logbook_adapter` 仅加载所需模块

**当前实现**: `src/engram/gateway/__init__.py` 已采用此模式。

### 原则 7: `public_api.py` 采用 Tier 分层延迟导入

> **详细文档**：完整的 Tier 分层定义、导出项清单、ImportError 消息格式规范，请参见：
> **[gateway_public_api_surface.md](./gateway_public_api_surface.md)**（插件作者导入指南单一入口）

**核心原则**:
- **Tier A**: 核心稳定层，直接导入，无外部依赖
- **Tier B**: 可选依赖层，延迟导入，失败时抛出含安装指引的 `ImportError`
- **Tier C**: 便捷/内部层，可能在次版本调整

---

## 依赖层次图

```
┌──────────────────────────────────────────────────────────────┐
│                    Gateway 入口层                             │
│  main.py / outbox_worker.py / reconcile_outbox.py            │
│  [必需依赖快速失败: sys.exit(1)]                              │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    生命周期管理层                              │
│  lifecycle.py                                                 │
│  [import-time 依赖必需, lifespan-time 优雅降级]               │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    核心适配层                                 │
│  logbook_adapter.py / evidence_store.py                      │
│  [必需依赖 raise ImportError]                                │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    协议/服务层                                │
│  mcp_rpc.py / services/actor_validation.py                   │
│  [可选依赖降级: None 或 Fallback]                             │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    Handler 层                                 │
│  handlers/evidence_upload.py                                  │
│  [request-time 延迟导入, 返回结构化错误]                       │
└──────────────────────────────────────────────────────────────┘
```

---

## 测试契约

### 测试可选依赖降级

```python
# tests/gateway/test_importerror_optional_deps_contract.py
def test_mcp_rpc_without_openmemory_client():
    """mcp_rpc 模块在 openmemory_client 不可用时应正常加载"""
    # 模拟 ImportError
    with patch.dict('sys.modules', {'engram.gateway.openmemory_client': None}):
        # 重新导入
        import importlib
        mcp_rpc = importlib.reload(engram.gateway.mcp_rpc)
        # 验证降级
        assert mcp_rpc.OpenMemoryError is None
```

### 测试结构化错误响应

```python
def test_evidence_upload_dependency_missing():
    """evidence_upload 在依赖缺失时返回 DEPENDENCY_MISSING"""
    with patch.dict('sys.modules', {'engram.gateway.evidence_store': None}):
        result = await execute_evidence_upload(
            content="test", content_type="text/plain", deps=mock_deps
        )
        assert result["ok"] is False
        assert result["error_code"] == "DEPENDENCY_MISSING"
        assert "engram_logbook" in result["message"]
```

### 测试统一错误格式

```python
def test_make_dependency_missing_error_required_fields():
    """验证 make_dependency_missing_error 返回所有必需字段"""
    from engram.gateway.mcp_rpc import make_dependency_missing_error

    result = make_dependency_missing_error(
        message="测试错误消息",
        suggestion="测试解决建议",
        missing_module="test_module",
    )

    # 验证必需字段存在
    assert "ok" in result
    assert "error_code" in result
    assert "retryable" in result
    assert "message" in result
    assert "suggestion" in result
    assert "details" in result

    # 验证字段语义
    assert result["ok"] is False
    assert result["error_code"] == "DEPENDENCY_MISSING"
    assert result["retryable"] is False
```

详见: `tests/gateway/test_importerror_optional_deps_contract.py::TestDependencyMissingErrorUnifiedFormat`

---

## 验收标准

### 必须通过的测试

修改 ImportError 处理或可选依赖相关代码后，**必须**运行以下测试：

```bash
# 1. ImportError 可选依赖契约测试（必须）
pytest tests/gateway/test_importerror_optional_deps_contract.py -v

# 2. DI 边界检查（必须）
python scripts/ci/check_gateway_di_boundaries.py --verbose

# 3. Gateway 启动测试（必须）
pytest tests/gateway/test_gateway_startup.py -v

# 4. 完整 Gateway 测试（推荐）
pytest tests/gateway/ -v

# 5. 验收测试（推荐）
pytest tests/acceptance/test_gateway_startup.py -v
```

### 验收检查清单

| # | 检查项 | 验证方式 | 通过标准 |
|---|--------|---------|---------|
| 1 | 入口层必需依赖缺失时快速失败 | `test_importerror_optional_deps_contract.py` | 返回非零退出码 + 用户友好错误消息 |
| 2 | 核心适配层必需依赖缺失时抛出 ImportError | 代码审查 + 契约测试 | 错误消息包含安装指引 |
| 3 | 协议层可选依赖缺失时降级为 None | `test_importerror_optional_deps_contract.py` | 模块正常加载，功能降级 |
| 4 | Handler 层可选依赖缺失时返回结构化错误 | `test_importerror_optional_deps_contract.py` | `error_code=DEPENDENCY_MISSING` |
| 5 | Handlers 不在 import-time 触发外部依赖 | `check_gateway_di_boundaries.py` | 无违规导入 |
| 6 | `create_app()` 在缺少可选依赖时仍能成功 | `test_gateway_startup.py` | 应用正常创建 |
| 7 | DEPENDENCY_MISSING 错误使用统一 helper | `TestDependencyMissingErrorUnifiedFormat` | 必需字段完整且语义正确 |

### CI 门禁要求

以下检查在 CI 中默认运行，任何失败将阻止合并：

| CI Job | 检查内容 | 失败处理 |
|--------|---------|---------|
| **gateway-di-boundaries** | handlers 是否违规导入隐式依赖 | 阻止合并 |
| **test (gateway/)** | ImportError 契约测试 + 启动测试 | 阻止合并 |
| **test (acceptance/)** | 端到端启动验收 | 阻止合并 |

### 本地完整预检命令

```bash
# 一键运行所有验收检查
python scripts/ci/check_gateway_di_boundaries.py --verbose && \
pytest tests/gateway/test_importerror_optional_deps_contract.py \
       tests/gateway/test_gateway_startup.py \
       tests/gateway/test_di_boundaries.py -v && \
pytest tests/acceptance/test_gateway_startup.py -v
```

### 测试与准则关联矩阵

| 测试类 | 验证的准则 | 覆盖模块 |
|--------|----------|---------|
| `TestImportErrorOptionalDepsContract` | 原则 1-4 | 所有 Gateway 模块 |
| `TestGatewayStartupDegradedMode` | 原则 1, 3 | `main.py`, `create_app()` |
| `TestDIBoundaries` | 原则 1, deps 一致化 | `handlers/**` |
| `TestEvidenceUploadDependencyMissing` | 原则 4 | `handlers/evidence_upload.py` |
| `TestDependencyMissingErrorUnifiedFormat` | 原则 4, 统一格式 | `mcp_rpc.py`, `handlers/*` |

---

## 插件作者注意事项

> **完整指南**：插件作者导入策略、Tier 分层详细定义、try/except 可复制代码片段、ImportError 消息格式契约，请参见：
> **[gateway_public_api_surface.md](./gateway_public_api_surface.md)**（单一入口文档）

### 核心原则

- **只依赖 Tier A 符号**：ports/错误码/RequestContext，无外部依赖且主版本内接口不变
- **Tier B 符号需检查依赖**：使用 try/except 模式检查 `ImportError`
- **避免 Tier C 符号**：便捷函数可能在次版本调整，使用 Tier A 替代方案

### 相关文档

| 文档 | 说明 |
|------|------|
| [gateway_public_api_surface.md](./gateway_public_api_surface.md) | **插件作者导入指南单一入口** — Tier 分层定义、导出项分析、可复制代码片段 |
| [gateway_contract_convergence.md §11](../contracts/gateway_contract_convergence.md#11-public-api-向后兼容策略) | 向后兼容策略不变量 |
| [mcp_jsonrpc_error_v2.md](../contracts/mcp_jsonrpc_error_v2.md) | 错误码契约 |

---

## 相关文档

- [ADR: Gateway DI 与入口边界统一](./adr_gateway_di_and_entry_boundary.md)
- [Gateway 设计文档](../gateway/06_gateway_design.md)
- [环境变量参考](../reference/environment_variables.md)
- [Gateway Public API Surface](./gateway_public_api_surface.md)
