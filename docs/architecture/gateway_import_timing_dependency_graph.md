# Gateway 导入时机与依赖图

> 状态: **稳定**  
> 创建日期: 2026-02-01  
> 相关文档:
> - [Gateway 模块边界与 Import 规则](./gateway_module_boundaries.md)
> - [Gateway ImportError 与可选依赖处理规范](./gateway_importerror_and_optional_deps.md)

---

## 1. 概述

本文档定义 `src/engram/gateway/` 目录下各关键模块的**导入时机**、**依赖可选性**及其在依赖图中的位置。为新增模块提供"应放在哪一层"的决策 checklist。

**核心原则**：

- **Import-Safe**: 顶层模块导入时不触发 `get_config()`/`get_container()`
- **延迟初始化**: 重量级依赖在 lifespan 或 request-time 时才加载
- **优雅降级**: 可选依赖缺失时返回结构化错误，不阻止服务启动

---

## 2. 导入时机定义

| 时机 | 代码层级 | 触发条件 | 示例 |
|------|---------|---------|------|
| **import-time** | 模块顶层 | 模块被 `import` 时立即执行 | `from .logbook_adapter import get_adapter` |
| **create_app-time** | app.py 内 | `create_app()` 函数执行时 | middleware 安装、routes 注册 |
| **register_routes-time** | routes.py 内 | `register_routes(app)` 被调用时 | handlers、mcp_rpc 延迟导入 |
| **lifespan-time** | lifecycle.py 内 | FastAPI lifespan 上下文启动时 | `get_config()`, `get_container()`, 依赖预热 |
| **request-time** | handler/service 内 | HTTP 请求处理时（函数内延迟导入） | `from ..evidence_store import ...` |
| **CLI-startup-time** | main.py 内 | `main()` 函数执行时（CLI 启动） | engram.logbook 模块检查、配置预检查 |

### 2.1 时机优先级

```
import-time → create_app-time → lifespan-time → request-time
    ↑                                              ↓
    └─── CLI-startup-time（独立路径，仅 CLI 触发）────┘
```

---

## 3. 关键模块导入时机汇总

### 3.1 入口层模块（ENTRYPOINTS）

| 模块 | 文件 | 导入时机 | import-time 依赖 | 延迟导入的依赖 | 可选性 |
|------|------|---------|-----------------|---------------|--------|
| **`__init__.py`** | `__init__.py` | import-time | 无（懒加载策略） | `logbook_adapter`, `openmemory_client`, `outbox_worker` | 全部可选 |
| **`main.py`** | `main.py` | import-time + CLI-startup-time | `app.create_app`, `lifecycle.lifespan` | `engram.logbook.errors`, `config.*`, `logbook_adapter`, `startup.*` | 延迟依赖必需 |
| **`app.py`** | `app.py` | create_app-time | `api_models.*`, `config.GatewayConfig`, `container.*` | `middleware.install_middleware`, `routes.register_routes` | 必需 |
| **`routes.py`** | `routes.py` | register_routes-time | `api_models.*` | `dependencies.*`, `handlers.*`, `mcp_rpc.*`, `entrypoints.tool_executor` | 延迟依赖必需 |
| **`middleware.py`** | `middleware.py` | create_app-time + request-time | `fastapi`, `starlette` | `mcp_rpc.generate_correlation_id` | 必需 |
| **`lifecycle.py`** | `lifecycle.py` | import-time + lifespan-time | `config.*`, `container.*`, `startup.*` | `get_config()`, `get_container()` 调用 | import-time 必需，lifespan-time 优雅降级 |
| **`dependencies.py`** | `dependencies.py` | request-time | 无（全部延迟导入） | `container.get_container`, `mcp_rpc.*`, `middleware.*` | 必需 |

### 3.2 入口子层模块（entrypoints/）

| 模块 | 文件 | 导入时机 | import-time 依赖 | 延迟导入的依赖 | 可选性 |
|------|------|---------|-----------------|---------------|--------|
| **`tool_executor`** | `entrypoints/tool_executor.py` | request-time | 无 | `handlers.*`, `logbook_adapter.get_reliability_report` | handlers 必需，reliability_report 可选 |

### 3.3 业务逻辑层模块（handlers/）

| 模块 | 文件 | 导入时机 | import-time 依赖 | 延迟导入的依赖 | 可选性 |
|------|------|---------|-----------------|---------------|--------|
| **`memory_store`** | `handlers/memory_store.py` | import-time | `di.*`, `mcp_rpc.*`, `policy.*` | 无 | 必需 |
| **`memory_query`** | `handlers/memory_query.py` | import-time | `di.*`, `mcp_rpc.*` | 无 | 必需 |
| **`governance_update`** | `handlers/governance_update.py` | import-time | `di.*`, `mcp_rpc.*` | 无 | 必需 |
| **`evidence_upload`** | `handlers/evidence_upload.py` | **request-time** | 无 | `evidence_store.*` | **可选**（最佳实践示例） |

### 3.4 服务层模块（services/）

| 模块 | 文件 | 导入时机 | import-time 依赖 | 延迟导入的依赖 | 可选性 |
|------|------|---------|-----------------|---------------|--------|
| **`actor_validation`** | `services/actor_validation.py` | import-time | `engram.logbook.errors.ErrorCode`（可选，有 Fallback） | 无 | ErrorCode 可选 |
| **`audit_service`** | `services/audit_service.py` | import-time | `audit_event.*`, `logbook_adapter.*` | 无 | 必需 |
| **`hash_utils`** | `services/hash_utils.py` | import-time | 标准库 | 无 | 必需 |
| **`ports`** | `services/ports.py` | import-time | 标准库 typing | 无 | 必需 |

### 3.5 基础设施层模块（INFRA）

| 模块 | 文件 | 导入时机 | import-time 依赖 | 延迟导入的依赖 | 可选性 |
|------|------|---------|-----------------|---------------|--------|
| **`logbook_adapter`** | `logbook_adapter.py` | import-time | `engram.logbook.*` 核心模块 | `engram.logbook.migrate`（可选） | 核心必需，migrate 可选 |
| **`evidence_store`** | `evidence_store.py` | import-time | `engram.logbook.artifact_store`, `engram.logbook.db`, `engram.logbook.uri` | 无 | 必需 |
| **`openmemory_client`** | `openmemory_client.py` | import-time | 标准库 + httpx | 无 | 必需 |
| **`config`** | `config.py` | import-time | 标准库 + pydantic | 无 | 必需 |
| **`container`** | `container.py` | import-time | `config.*`, `di.*` | 无 | 必需 |
| **`di`** | `di.py` | import-time | 标准库 + typing | 无 | 必需 |

### 3.6 协议层模块

| 模块 | 文件 | 导入时机 | import-time 依赖 | 延迟导入的依赖 | 可选性 |
|------|------|---------|-----------------|---------------|--------|
| **`mcp_rpc`** | `mcp_rpc.py` | import-time | 标准库 + pydantic | `openmemory_client` 异常类（可选）, `logbook_adapter.LogbookDBCheckError`（可选） | 核心必需，异常类可选 |

### 3.7 Worker 模块

| 模块 | 文件 | 导入时机 | import-time 依赖 | 延迟导入的依赖 | 可选性 |
|------|------|---------|-----------------|---------------|--------|
| **`outbox_worker`** | `outbox_worker.py` | import-time | `engram.logbook.errors.ErrorCode` | 无 | **必需**（缺失时 `sys.exit(1)`） |
| **`reconcile_outbox`** | `reconcile_outbox.py` | import-time | `engram.logbook.errors.ErrorCode` | 无 | **必需**（缺失时 `sys.exit(1)`） |

---

## 4. 依赖图

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              __init__.py (懒加载)                                │
│                                    │                                            │
│                    ┌───────────────┼───────────────┐                            │
│                    ▼               ▼               ▼                            │
│            logbook_adapter  openmemory_client  outbox_worker                    │
│            (首次访问时导入)   (首次访问时导入)   (首次访问时导入)                    │
└─────────────────────────────────────────────────────────────────────────────────┘
                                     │
┌─────────────────────────────────────────────────────────────────────────────────┐
│                               main.py (入口)                                     │
│   import-time: app.create_app, lifecycle.lifespan                               │
│   CLI-startup-time: engram.logbook.errors, config.*, logbook_adapter, startup.* │
└────────────────────────────────────┬────────────────────────────────────────────┘
                                     │ create_app(lifespan=lifespan)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                               app.py (工厂)                                      │
│   create_app-time: middleware.install_middleware, routes.register_routes        │
│   ❌ 不调用 get_config()/get_container()（除非显式传入 config/container）          │
└────────────────────────────────────┬────────────────────────────────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────────────┐
│   middleware.py     │  │    routes.py        │  │       lifecycle.py          │
│ create_app-time:    │  │ register_routes-time│  │ lifespan-time:              │
│ - 安装中间件        │  │ 延迟导入:            │  │ - get_config()              │
│ request-time:       │  │ - dependencies.*    │  │ - validate_config()         │
│ - generate_corr_id  │  │ - handlers.*        │  │ - GatewayContainer.create() │
└─────────────────────┘  │ - mcp_rpc.*         │  │ - check_logbook_db()        │
                         │ - entrypoints.*     │  │ - deps 预热                  │
                         └──────────┬──────────┘  └───────────────┬─────────────┘
                                    │                             │
              ┌─────────────────────┼─────────────────────────────┼───────────┐
              ▼                     ▼                             ▼           ▼
┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────┐  ┌─────────┐
│  dependencies.py    │  │entrypoints/         │  │   handlers/     │  │container│
│ request-time:       │  │tool_executor.py     │  │ import-time:    │  │ .py     │
│ - get_container()   │  │ request-time:       │  │ - di.*          │  │get_     │
│ - generate_corr_id  │  │ - handlers.*        │  │ - mcp_rpc.*     │  │container│
└─────────────────────┘  │ - logbook_adapter   │  │ - policy.*      │  └────┬────┘
                         └─────────────────────┘  │ request-time:   │       │
                                                  │ (evidence_      │       ▼
                                                  │  upload only)   │  ┌─────────┐
                                                  └────────┬────────┘  │  di.py  │
                                                           │           │GatewayDeps
                                                           ▼           └─────────┘
                                            ┌─────────────────────────────┐
                                            │         services/           │
                                            │ import-time:                │
                                            │ - actor_validation (可选)   │
                                            │ - audit_service (必需)      │
                                            │ - hash_utils (必需)         │
                                            │ - ports (必需)              │
                                            └──────────────┬──────────────┘
                                                           │
                                                           ▼
                                            ┌─────────────────────────────┐
                                            │           INFRA             │
                                            │ import-time:                │
                                            │ - logbook_adapter (必需)    │
                                            │ - evidence_store (必需)     │
                                            │ - openmemory_client (必需)  │
                                            │ - config (必需)             │
                                            └─────────────────────────────┘
```

---

## 5. 新增模块决策 Checklist

当你需要在 Gateway 中新增模块时，按照以下 checklist 确定模块应该放在哪一层。

### 5.1 层级决策流程图

```
开始：我要新增一个模块
         │
         ▼
   ┌─────────────────────────────┐
   │ 模块需要处理 HTTP 请求路由   │───是──▶ 放入 routes.py 或 entrypoints/
   │ 或协议解析？                │
   └─────────────────────────────┘
         │否
         ▼
   ┌─────────────────────────────┐
   │ 模块需要调用 get_config()   │───是──▶ 放入入口层：main.py/app.py/
   │ 或 get_container()？        │        lifecycle.py/dependencies.py
   └─────────────────────────────┘
         │否
         ▼
   ┌─────────────────────────────┐
   │ 模块是业务 API 的实现逻辑？ │───是──▶ 放入 handlers/
   └─────────────────────────────┘
         │否
         ▼
   ┌─────────────────────────────┐
   │ 模块是可复用的领域服务/     │───是──▶ 放入 services/
   │ 工具函数？                  │
   └─────────────────────────────┘
         │否
         ▼
   ┌─────────────────────────────┐
   │ 模块是外部系统适配器/       │───是──▶ 放入 INFRA 层（根目录）
   │ 数据库/客户端封装？         │        如 logbook_adapter.py
   └─────────────────────────────┘
```

### 5.2 入口层模块 Checklist

**适用场景**：HTTP 路由、协议解析、依赖获取、correlation_id 生成

| # | 检查项 | 通过标准 |
|---|--------|---------|
| 1 | 模块需要调用 `get_container()` 或 `get_config()` | ✅ 允许 |
| 2 | 模块需要调用 `generate_correlation_id()` | ✅ 允许 |
| 3 | 模块顶层导入是否会触发 `get_config()` | ❌ 应延迟到函数内 |
| 4 | 模块是否直接实现业务逻辑 | ❌ 应委托给 handlers |

**代码模式**：

```python
# entrypoints/my_feature.py - 正确示例
async def execute_my_feature(
    args: Dict[str, Any],
    *,
    correlation_id: str,
    get_deps: Callable[[], "GatewayDepsProtocol"],
) -> Dict[str, Any]:
    # 延迟导入 handlers
    from ..handlers import my_feature_impl
    
    deps = get_deps()
    return await my_feature_impl(..., correlation_id=correlation_id, deps=deps)
```

### 5.3 Handlers 层模块 Checklist

**适用场景**：API 业务逻辑实现、请求参数校验、响应构建

| # | 检查项 | 通过标准 |
|---|--------|---------|
| 1 | 模块不调用 `get_container()`/`get_config()`/`generate_correlation_id()` | ✅ |
| 2 | 依赖通过 `deps: GatewayDeps` 参数传入 | ✅ |
| 3 | correlation_id 通过参数传入 | ✅ |
| 4 | 可选依赖使用 request-time 延迟导入 | ✅ |
| 5 | 缺失依赖时返回结构化错误（不抛出异常） | ✅ |

**代码模式**：

```python
# handlers/my_handler.py - 正确示例
from ..di import GatewayDeps

async def my_handler_impl(
    payload: str,
    correlation_id: str,  # 由入口层传入
    deps: GatewayDeps,    # 由入口层传入
) -> MyResponse:
    config = deps.config  # ✅ 从 deps 获取
    adapter = deps.logbook_adapter  # ✅ 从 deps 获取
    
    # 可选依赖使用 request-time 延迟导入
    try:
        from ..optional_module import optional_feature
    except ImportError:
        return MyResponse(ok=False, error_code="DEPENDENCY_MISSING", ...)
```

### 5.4 Services 层模块 Checklist

**适用场景**：可复用的领域服务、纯函数工具、跨 handler 共享逻辑

| # | 检查项 | 通过标准 |
|---|--------|---------|
| 1 | 模块不调用 `get_container()`/`get_config()` | ✅ |
| 2 | 可以通过 `deps` 参数获取依赖 | ✅ |
| 3 | 纯函数尽量无状态、无副作用 | ✅ |
| 4 | 可选依赖缺失时提供 Fallback | ✅（推荐） |

**代码模式**：

```python
# services/my_service.py - 正确示例

# 可选依赖使用 Fallback
try:
    from engram.logbook.errors import ErrorCode
except ImportError:
    class ErrorCode:  # Fallback
        MY_ERROR = "my_error"

def my_utility_function(data: str) -> str:
    """纯函数，无外部依赖"""
    return data.upper()

def my_service_function(deps: GatewayDeps, ...) -> Result:
    """通过 deps 获取依赖"""
    return deps.logbook_adapter.query(...)
```

### 5.5 INFRA 层模块 Checklist

**适用场景**：外部系统适配器、数据库封装、HTTP 客户端

| # | 检查项 | 通过标准 |
|---|--------|---------|
| 1 | 模块封装外部系统交互（DB、HTTP、消息队列等） | ✅ |
| 2 | 必需依赖缺失时 `raise ImportError` + 安装指引 | ✅ |
| 3 | 可选依赖缺失时设置 `_FEATURE_AVAILABLE = False` | ✅ |
| 4 | 被 `GatewayDeps` 封装，不直接被 handlers 导入 | ✅ |

**代码模式**：

```python
# my_adapter.py - 正确示例

# 必需依赖
try:
    from external_package import Client
except ImportError as e:
    raise ImportError(
        f'my_adapter 需要 external_package: {e}\n'
        '请先安装:\n  pip install external_package'
    )

# 可选依赖
_OPTIONAL_FEATURE_AVAILABLE = False
try:
    from external_package.optional import OptionalFeature
    _OPTIONAL_FEATURE_AVAILABLE = True
except ImportError:
    pass

class MyAdapter:
    def query(self, ...):
        if _OPTIONAL_FEATURE_AVAILABLE:
            return OptionalFeature.query(...)
        return self._fallback_query(...)
```

---

## 6. 导入时机最佳实践

### 6.1 保持 Import-Safe

**目标**：`from engram.gateway.main import app` 不依赖环境变量

| 模块 | 策略 |
|------|------|
| `main.py` | 顶层只导入 `create_app`, `lifespan`；CLI 专用导入放在 `main()` 内 |
| `app.py` | 不显式传参时，不调用 `get_container()`/`get_config()` |
| `routes.py` | 所有 handlers/mcp_rpc 导入放在 `register_routes()` 内 |
| `middleware.py` | `generate_correlation_id` 在 `dispatch()` 内延迟导入 |
| `dependencies.py` | 所有导入放在函数内（request-time） |

### 6.2 延迟导入模式

```python
# ✅ 正确：函数内延迟导入
def my_function():
    from .heavy_module import heavy_function
    return heavy_function()

# ❌ 错误：顶层导入触发依赖链
from .heavy_module import heavy_function  # import-time 触发

def my_function():
    return heavy_function()
```

### 6.3 可选依赖降级模式

```python
# 模式 A：布尔标志
_FEATURE_AVAILABLE = False
try:
    from optional_module import feature
    _FEATURE_AVAILABLE = True
except ImportError:
    pass

# 模式 B：设置为 None
OptionalClass = None
try:
    from optional_module import OptionalClass
except ImportError:
    pass

# 模式 C：Fallback 类
try:
    from optional_module import ErrorCode
except ImportError:
    class ErrorCode:
        DEFAULT = "default"
```

---

## 7. 验收标准

新增模块前，确保满足以下验收标准：

### 7.1 必须通过的检查

```bash
# 1. DI 边界检查（必须）
make check-gateway-di-boundaries

# 2. ImportError 契约测试（必须）
pytest tests/gateway/test_importerror_optional_deps_contract.py -v

# 3. Import-Safe 检查（必须）
pytest tests/gateway/test_import_safe_entrypoints.py -v

# 4. 完整 Gateway 测试（推荐）
pytest tests/gateway/ -v
```

### 7.2 验收 Checklist

| # | 检查项 | 验证方式 |
|---|--------|---------|
| 1 | 新模块不在 import-time 触发 `get_config()`/`get_container()` | 代码审查 + 测试 |
| 2 | handlers/services 不调用禁止的全局函数 | `check_gateway_di_boundaries.py` |
| 3 | 可选依赖缺失时优雅降级（不 crash） | `test_importerror_optional_deps_contract.py` |
| 4 | `from engram.gateway.main import app` 无环境变量依赖 | `test_import_safe_entrypoints.py` |
| 5 | 新模块放置在正确的层级 | 代码审查 |

---

## 8. 相关文档

| 文档 | 说明 |
|------|------|
| [Gateway 模块边界与 Import 规则](./gateway_module_boundaries.md) | DI 边界、禁止/允许的 import 模式 |
| [Gateway ImportError 与可选依赖处理规范](./gateway_importerror_and_optional_deps.md) | 各模块的 ImportError 处理详情 |
| [ADR: Gateway DI 与入口边界统一](./adr_gateway_di_and_entry_boundary.md) | 设计决策与迁移计划 |
| [v1.0 升级指南](../gateway/upgrade_v1_0_remove_handler_di_compat.md) | Legacy 参数移除与迁移清单 |

---

## 9. 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-02-01 | v1.0 | 初始版本，整合模块边界与 ImportError 文档的导入时机信息 |

---

> 更新时间：2026-02-01
