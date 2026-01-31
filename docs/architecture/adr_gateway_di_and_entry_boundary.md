# ADR: Gateway 依赖注入与入口边界统一

> 状态: **草案**  
> 创建日期: 2026-01-31  
> 决策者: Engram Core Team

---

## 1. 目标

### 1.1 目标

1. **统一 correlation_id 生成位置**：确保每个请求只生成一次 correlation_id，从入口层传播到所有子调用
2. **显式化依赖注入**：将隐式的全局依赖改为显式的依赖注入，提高可测试性和可维护性
3. **明确入口边界**：定义清晰的入口层职责，统一 HTTP/MCP/REST 三种协议的处理模式

### 1.2 非目标

1. **不重构业务逻辑**：handlers 中的核心业务逻辑（策略决策、审计写入等）保持不变
2. **不改变对外契约**：HTTP 响应格式、MCP 协议、JSON-RPC 错误码等对外契约保持兼容
3. **不引入复杂框架**：不引入 FastAPI 的 Depends 等复杂 DI 框架，保持轻量实现
4. **不在此 ADR 中重构 Outbox Worker**：Outbox Worker 的 DI 改造可在后续 ADR 中处理

---

## 2. 当前现状

### 2.1 隐式依赖注入模式

当前 Gateway 采用隐式的依赖注入模式：

```python
# main.py - 通过全局函数注册执行器
register_tool_executor(_execute_tool)

# mcp_rpc.py - 使用全局变量存储执行器
_tool_executor: Optional[ToolExecutor] = None

def get_tool_executor() -> Optional[ToolExecutor]:
    return _tool_executor
```

handlers 通过模块导入获取依赖：

```python
# handlers/memory_store.py
from ..config import get_config           # 隐式依赖
from ..logbook_db import get_db           # 隐式依赖  
from ..openmemory_client import get_client  # 隐式依赖
```

**问题**：

| 问题 | 影响 |
|------|------|
| 测试时难以 mock 依赖 | 需要 patch 多个模块级函数 |
| 依赖关系不透明 | 难以追踪数据流和副作用 |
| 全局状态污染 | 并发测试可能互相干扰 |

### 2.2 correlation_id 多处生成

当前 correlation_id 在多个位置生成，传播路径不一致：

| 位置 | 生成时机 | 代码引用 |
|------|----------|----------|
| `main.py:mcp_endpoint()` | MCP 请求入口 | `correlation_id = generate_correlation_id()` |
| `mcp_rpc.py:dispatch()` | JSON-RPC 分发时 | `corr_id = correlation_id or generate_correlation_id()` |
| `handlers/memory_store.py:memory_store_impl()` | 业务处理开始 | `correlation_id = generate_correlation_id()` |
| `mcp_rpc.py:ErrorData.to_dict()` | 错误构造时 | `corr_id = self.correlation_id or generate_correlation_id()` |

**问题**：

```
请求流程中可能产生多个 correlation_id：

mcp_endpoint() 生成 corr-1
    │
    ├─→ dispatch() 使用 corr-1 ✓
    │
    └─→ memory_store_impl() 生成 corr-2 ✗ (覆盖了入口的 corr-1)
            │
            └─→ 审计记录使用 corr-2
```

**契约违反**：[`docs/gateway/07_capability_boundary.md`](../gateway/07_capability_boundary.md#correlation_id-统一规则) 定义了"每个请求只生成一次 correlation_id"的契约，当前实现违反此契约。

### 2.3 入口边界不统一

当前存在三种入口模式，处理逻辑分散：

| 入口 | 协议 | correlation_id 生成 | 调用链 |
|------|------|---------------------|--------|
| `/mcp` (JSON-RPC) | JSON-RPC 2.0 | `mcp_endpoint()` | mcp_endpoint → dispatch → handle_tools_call → _execute_tool → handler |
| `/mcp` (旧协议) | Legacy MCP | `mcp_endpoint()` | mcp_endpoint → _execute_tool → handler |
| `/memory/store` | REST | handler 内部 | memory_store_endpoint → handler |

**问题**：

- REST 端点（`/memory/store`）的 correlation_id 在 handler 内部生成，与 MCP 端点不一致
- 三种入口的依赖获取方式相同（都是隐式），但 correlation_id 传播路径不同
- 难以统一添加跨切面逻辑（如请求日志、性能监控）

---

## 3. 决策与备选方案对比

### 3.1 方案 A：入口层统一 + 参数透传（推荐）

**核心思路**：在入口层（main.py）统一生成 correlation_id 和获取依赖，通过参数透传给 handlers。

```python
# main.py - 入口层
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    # 1. 统一生成 correlation_id
    correlation_id = generate_correlation_id()
    
    # 2. 获取依赖（或延迟获取）
    ctx = RequestContext(
        correlation_id=correlation_id,
        config=get_config(),
        db=get_db(),
    )
    
    # 3. 透传到 dispatch
    response = await mcp_router.dispatch(rpc_request, ctx=ctx)

# handlers/memory_store.py - 接收 ctx 参数
async def memory_store_impl(
    payload_md: str,
    ctx: RequestContext,  # 显式接收上下文
    ...
) -> MemoryStoreResponse:
    # 使用 ctx 中的依赖，不再自行生成
    correlation_id = ctx.correlation_id
    config = ctx.config
    db = ctx.db
```

**优点**：

| 优点 | 说明 |
|------|------|
| correlation_id 单点生成 | 入口层生成后透传，保证唯一性 |
| 依赖显式传递 | 测试时可直接注入 mock 对象 |
| 迁移成本适中 | 可分阶段迁移，保持向后兼容 |
| 无框架依赖 | 仅使用 dataclass 或 TypedDict |

**缺点**：

| 缺点 | 说明 |
|------|------|
| 需要修改 handler 签名 | 所有 handler 需要增加 ctx 参数 |
| 调用链传递开销 | 每层调用都需要传递 ctx |

### 3.2 方案 B：ContextVar 方案

**核心思路**：使用 Python 的 `contextvars` 存储请求上下文，handlers 通过 ContextVar 获取。

```python
# context.py
from contextvars import ContextVar

_request_context: ContextVar[RequestContext] = ContextVar('request_context')

def get_current_context() -> RequestContext:
    return _request_context.get()

# main.py
async def mcp_endpoint(request: Request):
    correlation_id = generate_correlation_id()
    ctx = RequestContext(correlation_id=correlation_id, ...)
    token = _request_context.set(ctx)
    try:
        response = await mcp_router.dispatch(rpc_request)
    finally:
        _request_context.reset(token)

# handlers/memory_store.py - 通过 ContextVar 获取
async def memory_store_impl(...) -> MemoryStoreResponse:
    ctx = get_current_context()  # 隐式获取
    correlation_id = ctx.correlation_id
```

**优点**：

| 优点 | 说明 |
|------|------|
| 无需修改 handler 签名 | 迁移成本更低 |
| 符合 Python 异步模式 | ContextVar 是异步安全的 |

**缺点**：

| 缺点 | 说明 |
|------|------|
| 依赖仍是隐式的 | 测试时需要设置 ContextVar |
| 调试困难 | 依赖来源不透明 |
| 可能遗漏 reset | 需要确保 finally 正确执行 |

### 3.3 方案 C：FastAPI Depends 方案

**核心思路**：使用 FastAPI 的依赖注入系统。

```python
# dependencies.py
async def get_request_context(request: Request) -> RequestContext:
    correlation_id = generate_correlation_id()
    return RequestContext(correlation_id=correlation_id, ...)

# main.py
@app.post("/mcp")
async def mcp_endpoint(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
):
    ...
```

**优点**：

| 优点 | 说明 |
|------|------|
| FastAPI 原生支持 | 集成度高，测试工具完善 |
| 自动依赖管理 | 支持依赖缓存和生命周期 |

**缺点**：

| 缺点 | 说明 |
|------|------|
| 仅适用于 HTTP 端点 | MCP 工具调用链需要额外处理 |
| 学习曲线 | 团队需要熟悉 Depends 模式 |
| 过度设计 | 对于当前规模可能过重 |

### 3.4 方案对比矩阵

| 维度 | 方案 A（参数透传） | 方案 B（ContextVar） | 方案 C（Depends） |
|------|-------------------|---------------------|------------------|
| **correlation_id 单点生成** | ✓ 强保证 | ✓ 强保证 | ✓ 强保证 |
| **依赖显式性** | 高（参数可见） | 低（隐式获取） | 中（装饰器声明） |
| **测试便利性** | 高（直接传参） | 中（需设置 ContextVar） | 高（override_dependency） |
| **迁移成本** | 中（需改签名） | 低（无需改签名） | 高（需重构路由） |
| **代码侵入性** | 中 | 低 | 中 |
| **框架依赖** | 无 | 无 | FastAPI |

### 3.5 决策

**推荐采用方案 A：入口层统一 + 参数透传**

理由：

1. **符合显式优于隐式原则**：依赖通过参数传递，代码可读性和可测试性最高
2. **correlation_id 唯一性有强保证**：编译时即可发现遗漏传递的问题
3. **可分阶段迁移**：可以通过兼容层逐步迁移，不需要一次性重构
4. **无外部框架依赖**：保持代码轻量

---

## 4. 分层架构与依赖规则

### 4.1 分层图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        app.py (HTTP 入口层)                          │
│   - 创建 FastAPI 应用                                                │
│   - 生成 correlation_id（唯一生成点）                                 │
│   - 调用 get_container() 获取全局容器                                 │
│   - 注册路由，调用 handlers                                          │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ 依赖传递
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   container.py (依赖容器层)                          │
│   - GatewayContainer: 集中管理所有依赖的延迟初始化                     │
│   - get_container(): 全局单例入口                                    │
│   - get_gateway_deps(): 获取 GatewayDeps 实例（推荐生产路径）          │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ 依赖绑定
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       di.py (依赖注入层)                             │
│   - GatewayDeps: 纯 Python 依赖容器，实现 GatewayDepsProtocol         │
│   - RequestContext: 请求上下文 dataclass                             │
│   - for_testing(): 测试专用工厂方法                                   │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ 依赖注入
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     handlers/ (业务逻辑层)                           │
│   - memory_store_impl, memory_query_impl, ...                       │
│   - 接收 deps: GatewayDeps 参数                                      │
│   - 禁止直接导入 get_config/get_client/get_container 等              │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ 数据访问
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   logbook_adapter.py / services/ (服务层)            │
│   - LogbookAdapter: Logbook 数据库适配器                              │
│   - OpenMemoryClient: OpenMemory 客户端                              │
│   - 被 deps 封装，不直接被 handlers 导入                              │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 依赖创建/传递链路

```
app.py
  │
  ├─ 启动时: get_container() → GatewayContainer（延迟初始化）
  │    │
  │    ├─ container.config → get_config() → GatewayConfig
  │    ├─ container.db → get_db() → LogbookDatabase（已弃用）
  │    ├─ container.logbook_adapter → LogbookAdapter(dsn)
  │    └─ container.openmemory_client → OpenMemoryClient(base_url, api_key)
  │
  ├─ 请求处理时:
  │    │
  │    ├─ 生成 correlation_id = generate_correlation_id()
  │    │
  │    ├─ 获取 deps = get_gateway_deps()
  │    │    │
  │    │    └─ 内部: container.deps → GatewayDeps.from_container(container)
  │    │
  │    └─ 调用 handler:
  │         memory_store_impl(..., correlation_id=correlation_id)
  │              │
  │              ├─ deps.config → 委托给 container.config
  │              ├─ deps.logbook_adapter → 委托给 container.logbook_adapter
  │              └─ deps.openmemory_client → 委托给 container.openmemory_client
  │
  └─ 测试时:
       │
       └─ deps = GatewayDeps.for_testing(
               config=mock_config,
               logbook_adapter=mock_adapter,
               openmemory_client=mock_client,
           )
```

### 4.3 允许/禁止的 import 清单

#### 允许的导入（handlers 内）

| 模块 | 允许导入 | 说明 |
|------|----------|------|
| `di.py` | `GatewayDeps`, `RequestContext` | 类型注解和参数 |
| `mcp_rpc.py` | `ErrorData`, `ErrorCategory`, `ErrorReason` | 错误处理 |
| `audit_event.py` | `build_audit_event`, `build_evidence_refs_json` | 审计构建 |
| `policy.py` | `check_policy`, `PolicyResult` | 策略检查 |

#### 禁止的导入（handlers 内）

| 禁止导入 | 原因 | 替代方案 |
|----------|------|----------|
| `config.get_config()` | 隐式依赖 | `deps.config` |
| `openmemory_client.get_client()` | 隐式依赖 | `deps.openmemory_client` |
| `logbook_db.get_db()` | 隐式依赖（已弃用） | `deps.logbook_adapter` |
| `logbook_adapter.get_adapter()` | 隐式依赖 | `deps.logbook_adapter` |
| `container.get_container()` | 绕过 DI 层 | `deps` 参数 |
| `container.get_gateway_deps()` | 应由入口层调用 | `deps` 参数 |
| `mcp_rpc.generate_correlation_id()` | handlers 不应生成 | 入口层传入 `correlation_id` |

**Lint 规则建议**（可通过 ruff 或 custom linter 实施）：

```python
# .ruff.toml 或 lint 配置
[tool.ruff.per-file-ignores]
"src/engram/gateway/handlers/*.py" = [
    # 禁止导入全局获取函数
    # 需要 custom rule 或 pre-commit hook 检查
]
```

---

## 5. 迁移步骤（分阶段）

### 5.1 P0：锁定契约（当前阶段）

**目标**：锁定 DI 接口契约，确保后续迁移不破坏现有代码。

**已完成**：

- [x] 定义 `GatewayDeps` dataclass 和 `GatewayDepsProtocol`
- [x] 定义 `RequestContext` dataclass
- [x] 实现 `GatewayContainer` 与 `GatewayDeps` 的桥接（`from_container`）
- [x] 实现 `get_gateway_deps()` 全局获取函数
- [x] 实现 `GatewayDeps.for_testing()` 测试工厂方法

**契约测试**：

```python
# tests/gateway/test_gateway_startup.py
def test_gateway_deps_protocol_contract():
    """验证 GatewayDeps 实现 GatewayDepsProtocol"""
    deps = GatewayDeps.for_testing(config=mock_config)
    assert hasattr(deps, 'config')
    assert hasattr(deps, 'logbook_adapter')
    assert hasattr(deps, 'openmemory_client')
```

### 5.2 P1：入口层改造

**目标**：在入口层（app.py）统一依赖获取和 correlation_id 生成。

**任务清单**：

- [x] `app.py` 使用 `get_container()` 初始化容器
- [x] `mcp_endpoint()` 在入口生成 `correlation_id`
- [x] REST 端点（`/memory/store`, `/memory/query`）在入口生成 `correlation_id`
- [x] `_execute_tool()` 接收并传递 `correlation_id` 参数
- [ ] 完善 `lifespan` 中的依赖预热逻辑

### 5.3 P2：handlers 收敛

**目标**：逐步修改 handlers 签名，接收 `deps` 和 `correlation_id` 参数，移除内部隐式依赖。

**迁移顺序**（按依赖关系）：

1. `memory_store_impl` - 核心写入路径
2. `memory_query_impl` - 查询路径
3. `governance_update_impl` - 管理路径
4. `execute_evidence_upload` - 证据上传

**当前状态**：handlers 仍使用隐式依赖（`get_config()`, `get_db()` 等），需逐步改造。

**改造模板**：

```python
# handlers/memory_store.py - 改造后
async def memory_store_impl(
    payload_md: str,
    # ... 其他参数 ...
    correlation_id: str,              # 必需（入口层传入）
    deps: Optional[GatewayDeps] = None,  # 兼容期可选
) -> MemoryStoreResponse:
    # 兼容处理
    if deps is None:
        deps = get_gateway_deps()
    
    # 使用 deps 获取依赖
    config = deps.config
    adapter = deps.logbook_adapter
    client = deps.openmemory_client
    
    # 业务逻辑...
```

### 5.4 P3：清理/弃用移除

**目标**：移除兼容代码，强制显式依赖注入。

**任务清单**：

- [ ] 将 `deps` 参数改为必需（移除 `Optional`）
- [ ] 移除 handlers 中的 `if deps is None` 兼容分支
- [ ] 移除 `logbook_db.py` 模块（已弃用）
- [ ] 更新所有测试使用 `GatewayDeps.for_testing()`
- [ ] 添加 lint 规则禁止 handlers 导入隐式依赖

---

## 6. 最佳实践

### 6.1 生产环境

```python
# 生产路径：使用 get_gateway_deps() 获取绑定到全局容器的 deps
from engram.gateway.container import get_gateway_deps

deps = get_gateway_deps()
adapter = deps.logbook_adapter
client = deps.openmemory_client
```

### 6.2 单元测试

```python
# 测试路径：使用 GatewayDeps.for_testing() 注入 mock
from engram.gateway.di import GatewayDeps

deps = GatewayDeps.for_testing(
    config=mock_config,
    logbook_adapter=mock_adapter,
    openmemory_client=mock_client,
)
result = await memory_store_impl(..., deps=deps, correlation_id="corr-test000")
```

### 6.3 集成测试

```python
# 集成测试：使用 GatewayContainer.create_for_testing()
from engram.gateway.container import GatewayContainer, set_container

container = GatewayContainer.create_for_testing(
    config=real_config,
    logbook_adapter=real_adapter,  # 连接真实测试数据库
)
set_container(container)

# 测试完成后清理
reset_container()
```

---

## 7. 旧迁移步骤参考（已合并）

> 以下内容保留供参考，实际迁移计划已更新为 P0-P3 阶段。

### 7.1 Phase 1：定义 RequestContext（兼容期）

**目标**：定义上下文结构，不改变现有行为。

```python
# src/engram/gateway/context.py (新增)
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class RequestContext:
    """
    请求上下文
    
    包含请求级别的共享信息，从入口层传递到所有子调用。
    """
    correlation_id: str
    config: Optional[Any] = None  # GatewayConfig
    db: Optional[Any] = None      # LogbookDB
    
    # 延迟获取依赖（兼容期使用）
    def get_config(self):
        if self.config is None:
            from .config import get_config
            self.config = get_config()
        return self.config
    
    def get_db(self):
        if self.db is None:
            from .logbook_db import get_db
            self.db = get_db()
        return self.db
```

**任务清单**：

- [ ] 创建 `src/engram/gateway/context.py`
- [ ] 定义 `RequestContext` dataclass
- [ ] 添加单元测试

### 7.2 Phase 2：入口层改造（收敛期）

**目标**：在入口层统一生成 correlation_id 和 RequestContext。

```python
# main.py - 改造后
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    # 统一生成 correlation_id（单点生成）
    correlation_id = generate_correlation_id()
    
    # 创建请求上下文
    ctx = RequestContext(correlation_id=correlation_id)
    
    if is_jsonrpc_request(body):
        response = await mcp_router.dispatch(rpc_request, ctx=ctx)
    else:
        result = await _execute_tool(tool, args, ctx=ctx)
    ...

@app.post("/memory/store")
async def memory_store_endpoint(request: MemoryStoreRequest):
    # REST 端点也在入口生成 correlation_id
    correlation_id = generate_correlation_id()
    ctx = RequestContext(correlation_id=correlation_id)
    return await memory_store_impl(..., ctx=ctx)
```

**任务清单**：

- [ ] 修改 `mcp_endpoint()` 创建 RequestContext
- [ ] 修改 `mcp_router.dispatch()` 接收 ctx 参数
- [ ] 修改 `_execute_tool()` 接收并传递 ctx
- [ ] 修改 REST 端点创建 RequestContext
- [ ] 更新集成测试

### 7.3 Phase 3：handlers 签名改造（迁移期）

**目标**：逐步修改 handlers 接收 RequestContext，移除内部的 correlation_id 生成。

```python
# handlers/memory_store.py - 改造后
async def memory_store_impl(
    payload_md: str,
    target_space: Optional[str] = None,
    # ... 其他参数 ...
    ctx: Optional[RequestContext] = None,  # 兼容期：可选参数
) -> MemoryStoreResponse:
    # 兼容处理：如果未传入 ctx，使用旧逻辑
    if ctx is None:
        from ..mcp_rpc import generate_correlation_id
        correlation_id = generate_correlation_id()
        config = get_config()
        db = get_db()
    else:
        correlation_id = ctx.correlation_id
        config = ctx.get_config()
        db = ctx.get_db()
    
    # 业务逻辑保持不变
    ...
```

**迁移顺序**（按依赖关系）：

1. `memory_store_impl` - 核心路径
2. `memory_query_impl` - 查询路径
3. `governance_update_impl` - 管理路径
4. `execute_evidence_upload` - 证据上传

**任务清单**：

- [ ] 修改 `memory_store_impl` 签名
- [ ] 修改 `memory_query_impl` 签名
- [ ] 修改 `governance_update_impl` 签名
- [ ] 修改 `execute_evidence_upload` 签名
- [ ] 更新所有调用点
- [ ] 移除 handlers 中的 `generate_correlation_id()` 调用

### 7.4 Phase 4：清理与稳定（稳定期）

**目标**：移除兼容代码，强制要求 ctx 参数。

```python
# handlers/memory_store.py - 最终版本
async def memory_store_impl(
    payload_md: str,
    target_space: Optional[str] = None,
    # ... 其他参数 ...
    ctx: RequestContext,  # 必需参数
) -> MemoryStoreResponse:
    correlation_id = ctx.correlation_id
    config = ctx.get_config()
    db = ctx.get_db()
    ...
```

**任务清单**：

- [ ] 将 `ctx` 参数改为必需
- [ ] 移除兼容性的 `if ctx is None` 分支
- [ ] 更新文档
- [ ] 运行完整测试套件

---

## 8. 验收标准

### 8.1 correlation_id 单点生成验收

| 验收项 | 测试方法 | 测试文件 |
|--------|----------|----------|
| MCP 请求只生成一次 correlation_id | 在 handler 入口打桩验证 | `test_mcp_jsonrpc_contract.py` |
| REST 请求只生成一次 correlation_id | 检查审计记录 | `test_unified_stack_integration.py` |
| 错误响应包含正确的 correlation_id | 验证 error.data.correlation_id | `test_error_codes.py` |

**契约测试引用**：

- `test_mcp_jsonrpc_contract.py::TestCorrelationIdUnifiedContract`
- `test_error_codes.py::TestCorrelationIdInErrorResponses`

### 8.2 依赖注入验收

| 验收项 | 测试方法 |
|--------|----------|
| handlers 可接收 mock 的 RequestContext | 单元测试直接注入 |
| 不依赖全局状态即可测试 | 无需 patch 模块级函数 |
| 并发请求的 ctx 隔离 | 异步并发测试 |

### 8.3 现有契约保持

以下测试必须保持通过，不得因重构而失败：

| 测试文件 | 覆盖契约 |
|----------|----------|
| `test_mcp_jsonrpc_contract.py` | JSON-RPC 协议契约 |
| `test_audit_event_contract.py` | 审计事件结构契约 |
| `test_error_codes.py` | 错误码与 ErrorData 契约 |
| `test_unified_stack_integration.py` | 端到端集成契约 |
| `test_reconcile_outbox.py` | Audit ↔ Outbox 闭环契约 |

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 签名变更导致调用点遗漏 | 运行时错误 | 分阶段迁移 + 兼容期 |
| 并发测试干扰 | 测试不稳定 | RequestContext 隔离设计 |
| 性能影响 | ctx 对象创建开销 | dataclass 轻量，开销可忽略 |

---

## 10. 相关文档

| 文档 | 说明 |
|------|------|
| [Gateway 能力边界](../gateway/07_capability_boundary.md) | correlation_id 契约定义 |
| [Gateway 设计](../gateway/06_gateway_design.md) | 整体架构 |
| [ADR: Gateway 审计原子性](./adr_gateway_audit_atomicity.md) | 审计相关决策 |

---

## 11. 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-01-31 | v1.0 | 初始版本，推荐方案 A |
| 2026-01-31 | v1.1 | 新增：分层图、依赖链路、import 清单、P0-P3 迁移阶段、最佳实践 |
