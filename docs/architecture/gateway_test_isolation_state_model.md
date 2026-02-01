# Gateway 测试隔离状态模型

> **SSOT**: 本文档是 Gateway 模块级状态变量及其测试隔离策略的权威参考。
>
> **更新时间**: 2026-02-01

---

## 目录

1. [概述](#概述)
2. [状态变量详细清单](#状态变量详细清单)
3. [每类状态的 reset API](#每类状态的-reset-api)
4. [推荐测试写法（DI vs patch）](#推荐测试写法di-vs-patch)
5. [测试隔离规则](#测试隔离规则)
6. [Opt-out Marker 使用边界](#opt-out-marker-使用边界)
7. [Fixture 使用指南](#fixture-使用指南)
8. [sys.modules 注入测试](#sysmodules-注入测试)

---

## 测试模板（推荐写法）

> **SSOT**: 本节定义 Gateway 模块的三类标准测试模板，所有新测试必须遵循。

### 模板 1: Handler 单元测试（DI 注入）

**适用场景**: 测试 `handlers/` 或 `services/` 中的业务逻辑

**核心原则**: 只用 `GatewayDeps.for_testing()` + fake ports，不依赖全局状态

```python
import pytest
from engram.gateway.di import GatewayDeps
from engram.gateway.handlers.memory_store import memory_store_impl
from tests.gateway.fakes import FakeLogbookAdapter, FakeOpenMemoryClient

@pytest.mark.asyncio
async def test_memory_store_dedup_miss(fake_logbook_adapter, fake_openmemory_client, test_correlation_id):
    """Handler 单元测试：使用 DI 注入所有依赖"""
    # 1. 配置 fake 行为
    fake_logbook_adapter.configure_dedup_miss()
    fake_openmemory_client.configure_store_success(memory_id="mem_test_123")
    
    # 2. 创建 deps（DI 方式，严格模式）
    deps = GatewayDeps.for_testing(
        logbook_adapter=fake_logbook_adapter,
        openmemory_client=fake_openmemory_client,
    )
    
    # 3. 调用被测函数，显式传入 deps
    result = await memory_store_impl(
        payload_md="test content",
        correlation_id=test_correlation_id,
        deps=deps,  # ✅ 显式传入
    )
    
    # 4. 断言结果
    assert result["ok"] is True
    assert result["memory_id"] == "mem_test_123"
    
    # 5. 验证调用（可选）
    assert fake_logbook_adapter.dedup_check_called
    assert fake_openmemory_client.store_called
```

**关键点**:
- ✅ 使用 `GatewayDeps.for_testing()` 创建依赖
- ✅ 使用 `tests/gateway/fakes.py` 中的 Fake 类
- ✅ 显式传入 `deps` 参数
- ❌ 禁止调用 `get_container()`、`get_config()` 等全局获取函数
- ❌ 禁止使用 `@pytest.mark.no_singleton_reset`

### 模板 2: Routes/FastAPI 集成测试

**适用场景**: 测试 FastAPI 路由端点、中间件集成

**核心原则**: 使用 `gateway_test_app` fixture，不直接依赖全局 `app`

```python
import pytest
from fastapi.testclient import TestClient

def test_mcp_tools_list(gateway_test_app):
    """FastAPI 集成测试：使用 gateway_test_app fixture"""
    # 1. gateway_test_app 返回配置好的 TestClient
    client = gateway_test_app
    
    # 2. 发送请求
    response = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1,
    })
    
    # 3. 断言响应
    assert response.status_code == 200
    data = response.json()
    assert "result" in data
    assert "tools" in data["result"]


def test_mcp_with_custom_fakes(gateway_test_app_factory, fake_logbook_adapter):
    """FastAPI 集成测试：自定义 fake 行为"""
    # 1. 配置 fake
    fake_logbook_adapter.configure_dedup_hit(memory_id="existing_mem")
    
    # 2. 使用工厂创建带自定义配置的 TestClient
    client = gateway_test_app_factory(logbook_adapter=fake_logbook_adapter)
    
    # 3. 发送请求
    response = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "memory_store", "arguments": {"payload_md": "test"}},
        "id": 2,
    })
    
    # 4. 断言
    assert response.status_code == 200
```

**关键点**:
- ✅ 使用 `gateway_test_app` 或 `gateway_test_app_factory` fixture
- ✅ fixture 内部管理 `TestClient(app)` 的生命周期
- ❌ 禁止直接 `from engram.gateway.main import app` 后创建 TestClient
- ❌ 禁止直接调用 `set_container()` 设置全局容器

### 模板 3: Optional Deps / ImportError 测试

**适用场景**: 测试可选依赖缺失时的降级行为

**核心原则**: 使用 `sys_modules_patcher` fixture，不直接操作 `sys.modules`

```python
import pytest

@pytest.mark.asyncio
async def test_evidence_upload_dependency_missing(sys_modules_patcher):
    """测试可选依赖缺失时的降级行为"""
    # 1. 定义需要管理的模块列表
    modules = [
        "engram.gateway.evidence_store",
        "engram.gateway.handlers.evidence_upload",
    ]
    
    # 2. 创建 patcher（自动保存原始模块状态）
    patcher = sys_modules_patcher(modules)
    
    # 3. 注入失败的模块
    patcher.inject_failing_import(
        "engram.gateway.evidence_store",
        "No module named 'engram_logbook' (mocked)"
    )
    
    # 4. 清除 handler 缓存以触发重新导入
    patcher.remove_module("engram.gateway.handlers.evidence_upload")
    
    # 5. 重新导入并测试
    from engram.gateway.handlers.evidence_upload import execute_evidence_upload
    
    result = await execute_evidence_upload(
        content="test",
        content_type="text/plain",
        title="test.txt",
        correlation_id="corr-test1234567890ab",
    )
    
    # 6. 验证降级行为
    assert result["ok"] is False
    assert result["error_code"] == "DEPENDENCY_MISSING"
    
    # 7. 无需手动清理，fixture 会自动恢复 sys.modules
```

**关键点**:
- ✅ 使用 `sys_modules_patcher` fixture
- ✅ 使用 `patcher.inject_failing_import()` 注入失败模块
- ✅ 使用 `patcher.remove_module()` 清除缓存
- ❌ 禁止直接操作 `sys.modules[...] = ...`
- ❌ 禁止直接 `del sys.modules[...]`

---

## 禁止项（Anti-patterns）

> **重要**: 以下模式在 Gateway 测试中**严格禁止**，违规代码会被 CI 拦截或 Code Review 拒绝。

### 1. 禁止直接调用全局获取函数

| 禁止调用 | 替代方案 | 原因 |
|----------|----------|------|
| `get_container()` | `deps` 参数 | 绕过 DI 层，破坏测试隔离 |
| `get_config()` | `deps.config` | 隐式依赖全局状态 |
| `get_client()` | `deps.openmemory_client` | 隐式依赖全局状态 |
| `get_adapter()` | `deps.logbook_adapter` | 隐式依赖全局状态 |
| `get_gateway_deps()` | 入口层传入 `deps` | 应由 fixture 或入口层调用 |

```python
# ❌ 错误：直接调用全局获取函数
async def test_bad_example():
    from engram.gateway.container import get_container
    container = get_container()  # 禁止！
    deps = container.as_deps()
    ...

# ✅ 正确：使用 DI 注入
async def test_good_example(gateway_deps):
    result = await handler_impl(deps=gateway_deps)
    ...
```

### 2. 禁止直接操作 sys.modules

```python
# ❌ 错误：直接操作 sys.modules
import sys
sys.modules["engram.gateway.evidence_store"] = mock_module  # 禁止！
del sys.modules["engram.gateway.handlers.evidence_upload"]  # 禁止！

# ✅ 正确：使用 sys_modules_patcher fixture
def test_import_failure(sys_modules_patcher):
    patcher = sys_modules_patcher(["engram.gateway.evidence_store"])
    patcher.inject_failing_import("engram.gateway.evidence_store", "mocked error")
    ...
```

### 3. 禁止在单元测试中使用 no_singleton_reset

```python
# ❌ 错误：单元测试使用 no_singleton_reset
@pytest.mark.no_singleton_reset  # 禁止！
def test_unit_something():
    ...

# ✅ 正确：仅在 integration/E2E 测试中使用
@pytest.mark.gate_profile("full")
@pytest.mark.integration
@pytest.mark.no_singleton_reset  # 允许
def test_e2e_with_shared_pool():
    ...
```

### 4. 禁止直接 import 并使用全局 app

```python
# ❌ 错误：直接使用全局 app
from engram.gateway.main import app
from fastapi.testclient import TestClient

def test_bad_fastapi():
    with TestClient(app) as client:  # 禁止！
        ...

# ✅ 正确：使用 gateway_test_app fixture
def test_good_fastapi(gateway_test_app):
    client = gateway_test_app
    response = client.post("/mcp", json={...})
    ...
```

### 5. 禁止直接设置全局容器

```python
# ❌ 错误：直接设置全局容器
from engram.gateway.container import set_container, GatewayContainer

def test_bad_container_setup():
    container = GatewayContainer.create_for_testing(...)
    set_container(container)  # 禁止！
    ...

# ✅ 正确：使用 gateway_test_container fixture
def test_good_container_setup(gateway_test_container):
    # fixture 已设置好容器
    ...
```

### 禁止项汇总表

| 禁止模式 | 检测方式 | 替代方案 |
|----------|----------|----------|
| `get_container()` in handlers/services | `check_gateway_di_boundaries` 门禁 | `deps` 参数 |
| `sys.modules[...] = ...` | Code Review | `sys_modules_patcher` |
| `no_singleton_reset` in unit tests | `test_opt_out_policy_contract.py` | 不使用或标记为 integration |
| 直接 `TestClient(app)` | Code Review | `gateway_test_app` fixture |
| 直接 `set_container()` | Code Review | `gateway_test_container` fixture |
| `deps is None` 兼容分支 | `check_gateway_di_boundaries` 门禁 | 移除兼容分支 |

---

## 概述

Gateway 模块包含多个全局状态变量，用于单例管理和请求上下文传递。为确保测试隔离、避免顺序依赖和 flaky 测试，需要在每个测试前后正确重置这些状态。

### 状态分类

| 分类 | 特点 | 代表 | 重置时机 |
|------|------|------|----------|
| **纯单例** | 模块级全局变量，惰性初始化 | `_container`, `_config`, `_adapter_instance`, `_default_client` | 每测试前后 |
| **实例缓存** | 依附于单例实例的内部缓存 | `GatewayContainer._deps_cache` | 随宿主单例重置 |
| **ContextVar** | 请求级上下文变量，协程隔离 | `_current_correlation_id`, `_request_correlation_id` | 每测试后 |
| **全局注册器** | 模块级注册表，由启动代码填充 | `_tool_executor` | 每测试前后 |
| **globals 缓存** | `__getattr__` 懒加载缓存 | `engram.gateway.__getattr__` | 一般无需重置 |

---

## 状态变量详细清单

### 1. 纯单例（模块级全局变量）

| 状态名 | 位置 | 类型 | 默认初始化点 | reset API |
|--------|------|------|--------------|-----------|
| `_container` | `container.py:252` | `Optional[GatewayContainer]` | `get_container()` 首次调用 | `reset_container()` |
| `_config` | `config.py:252` | `Optional[GatewayConfig]` | `get_config()` → `load_config()` | `reset_config()` |
| `_adapter_instance` | `logbook_adapter.py:1535` | `Optional[LogbookAdapter]` | `get_adapter()` 首次调用 | `reset_adapter()` |
| `_default_client` | `openmemory_client.py:509` | `Optional[OpenMemoryClient]` | `get_client()` 无参数调用 | `reset_client()` |

### 2. 实例缓存（依附于单例）

| 状态名 | 宿主 | 类型 | 说明 |
|--------|------|------|------|
| `GatewayContainer._deps_cache` | `_container` | `Optional[GatewayDepsProtocol]` | 缓存的 `GatewayDeps` 实例，通过 `as_deps()` 创建 |

**重置方式**: 调用 `container.reset()` 或 `reset_container()` 时自动清除。

### 3. ContextVar 状态

| 状态名 | 位置 | 类型 | 设置点 | reset API |
|--------|------|------|--------|-----------|
| `_current_correlation_id` | `mcp_rpc.py:357-359` | `ContextVar[Optional[str]]` | `dispatch()` → `set_current_correlation_id()` | `reset_current_correlation_id_for_testing()` |
| `_request_correlation_id` | `middleware.py:39-41` | `ContextVar[Optional[str]]` | `CorrelationIdMiddleware.dispatch()` | `reset_request_correlation_id_for_testing()` |

**ContextVar 特性**:
- 协程隔离，不同协程有独立的上下文副本
- `set()` 返回 Token，可通过 `reset(token)` 恢复
- 测试中使用 `reset_xxx_for_testing()` 显式重置为 None

### 4. 全局注册器

| 状态名 | 位置 | 类型 | 注册点 | reset API |
|--------|------|------|--------|-----------|
| `_tool_executor` | `mcp_rpc.py:1346` | `Optional[ToolExecutor]` | `register_tool_executor()` (main.py 启动时) | `reset_tool_executor_for_testing()` |

### 5. globals 缓存（`__getattr__` 懒加载）

| 模块 | 缓存位置 | 缓存内容 | 是否需重置 |
|------|----------|----------|------------|
| `engram/gateway/__init__.py:64` | `globals()[name]` | 懒加载的子模块 (`logbook_adapter`, `openmemory_client`, `outbox_worker`) | 一般无需 |
| `mcp_rpc.py:1884-1895` | 延迟导入 | `api_models` 中的严格模型 | 一般无需 |

**说明**: globals 缓存是模块级持久状态，在进程生命周期内不变。测试中一般不需要重置，除非需要测试模块导入失败场景（使用 `SysModulesPatcher`）。

---

## 每类状态的 reset API

### 统一入口

```python
# 推荐：使用统一入口重置所有状态
from engram.gateway.testing import reset_gateway_runtime_state

reset_gateway_runtime_state()  # 重置所有运行时状态
```

### 分层 reset API

```python
# 1. 重置所有单例（包括 tool_executor）
from engram.gateway.container import reset_all_singletons
reset_all_singletons()

# 2. 仅重置单个组件
from engram.gateway.container import reset_container
from engram.gateway.config import reset_config
from engram.gateway.logbook_adapter import reset_adapter
from engram.gateway.openmemory_client import reset_client
from engram.gateway.mcp_rpc import reset_tool_executor_for_testing

reset_container()   # 重置 _container 和 _config
reset_config()      # 仅重置 _config
reset_adapter()     # 重置 _adapter_instance
reset_client()      # 重置 _default_client
reset_tool_executor_for_testing()  # 重置 _tool_executor

# 3. 重置 ContextVar 状态
from engram.gateway.mcp_rpc import reset_current_correlation_id_for_testing
from engram.gateway.middleware import reset_request_correlation_id_for_testing

reset_current_correlation_id_for_testing()   # mcp_rpc 的 correlation_id
reset_request_correlation_id_for_testing()   # middleware 的 correlation_id
```

### reset 调用层级图

```
reset_gateway_runtime_state()  [testing/reset.py]
├── reset_all_singletons()     [container.py]
│   ├── _container.reset()     → 清除 _deps_cache
│   ├── _container = None
│   ├── reset_config()
│   ├── reset_adapter()
│   ├── reset_client()
│   └── reset_tool_executor_for_testing()
├── reset_current_correlation_id_for_testing()   [mcp_rpc.py]
└── reset_request_correlation_id_for_testing()   [middleware.py]
```

---

## 推荐测试写法（DI vs patch）

### 原则：优先 DI，避免 patch

| 场景 | 推荐方式 | 不推荐方式 |
|------|----------|------------|
| Handler 单元测试 | `GatewayDeps.for_testing()` | `@patch` 模块级函数 |
| FastAPI 集成测试 | `gateway_test_container` fixture | 直接操作全局单例 |
| ContextVar 测试 | `set_xxx()` + `reset_xxx_for_testing()` | `@patch` ContextVar |
| 模块导入失败测试 | `SysModulesPatcher` | 手动操作 `sys.modules` |

### 示例 1: Handler 单元测试（推荐：DI）

```python
@pytest.mark.asyncio
async def test_memory_store_with_di(fake_logbook_adapter, fake_openmemory_client, test_correlation_id):
    """使用 DI 注入依赖的单元测试"""
    from engram.gateway.di import GatewayDeps
    from engram.gateway.handlers.memory_store import memory_store_impl
    
    # 配置 fake 行为
    fake_logbook_adapter.configure_dedup_miss()
    fake_openmemory_client.configure_store_success(memory_id="mem_test_123")
    
    # 创建 deps（DI 方式）
    deps = GatewayDeps.for_testing(
        logbook_adapter=fake_logbook_adapter,
        openmemory_client=fake_openmemory_client,
    )
    
    # 调用被测函数
    result = await memory_store_impl(
        payload_md="test content",
        correlation_id=test_correlation_id,
        deps=deps,
    )
    
    assert result["ok"] is True
    assert result["memory_id"] == "mem_test_123"
```

### 示例 2: Handler 单元测试（不推荐：patch）

```python
# ❌ 不推荐：使用 patch 模拟全局状态
@pytest.mark.asyncio
async def test_memory_store_with_patch():
    from unittest.mock import patch, AsyncMock
    
    with patch("engram.gateway.handlers.memory_store.logbook_adapter") as mock_adapter:
        mock_adapter.check_dedup.return_value = None
        # ... 测试代码
        
    # 问题：
    # 1. 需要知道内部实现细节（导入路径）
    # 2. 测试脆弱，重构后易失效
    # 3. 难以组合多个 mock
```

### 示例 3: FastAPI 集成测试

```python
def test_mcp_endpoint(gateway_test_container):
    """使用 gateway_test_container fixture 的集成测试"""
    from engram.gateway.main import app
    from fastapi.testclient import TestClient
    
    # gateway_test_container 已设置全局容器
    with TestClient(app) as client:
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1,
        })
    
    assert response.status_code == 200
    data = response.json()
    assert "result" in data
    assert "tools" in data["result"]
```

### 示例 4: ContextVar 测试

```python
@pytest.mark.asyncio
async def test_correlation_id_propagation():
    """测试 correlation_id 上下文传递"""
    from engram.gateway.mcp_rpc import (
        set_current_correlation_id,
        get_current_correlation_id,
        reset_current_correlation_id_for_testing,
    )
    
    try:
        # 设置测试值
        token = set_current_correlation_id("corr-test1234567890ab")
        
        # 验证传递
        assert get_current_correlation_id() == "corr-test1234567890ab"
        
        # 可嵌套设置
        token2 = set_current_correlation_id("corr-nested123456789a")
        assert get_current_correlation_id() == "corr-nested123456789a"
        
        # 恢复
        reset_current_correlation_id_for_testing()
        assert get_current_correlation_id() is None
    finally:
        # 确保清理（auto_reset_gateway_state 也会处理）
        reset_current_correlation_id_for_testing()
```

### 示例 5: 自定义配置测试

```python
@pytest.mark.asyncio
async def test_with_custom_config():
    """测试自定义配置场景"""
    from engram.gateway.config import GatewayConfig
    from engram.gateway.di import GatewayDeps
    from tests.gateway.fakes import FakeLogbookAdapter, FakeOpenMemoryClient
    
    # 创建自定义配置
    custom_config = GatewayConfig(
        project_key="test-project",
        postgres_dsn="postgresql://test@localhost/test",
        openmemory_base_url="http://mock:8080",
        unknown_actor_policy="reject",  # 自定义策略
    )
    
    # 注入自定义配置
    deps = GatewayDeps.for_testing(
        config=custom_config,
        logbook_adapter=FakeLogbookAdapter(),
        openmemory_client=FakeOpenMemoryClient(),
    )
    
    # 验证配置生效
    assert deps.config.unknown_actor_policy == "reject"
```

---

## 测试隔离规则

### 核心原则：每测默认复位 + 显式 opt-out

```
┌─────────────────────────────────────────────────────────────────┐
│                     测试隔离策略                                  │
├─────────────────────────────────────────────────────────────────┤
│  默认行为: 每个测试前后自动重置所有 Gateway 全局状态              │
│  排除方式: 使用 @pytest.mark.no_singleton_reset marker          │
└─────────────────────────────────────────────────────────────────┘
```

#### 动机

1. **避免顺序依赖**: 测试 A 的状态不应影响测试 B 的结果
2. **防止 Flaky 测试**: 并行运行时全局状态污染是 flaky 的主要来源
3. **显式优于隐式**: 需要复用状态的测试必须显式声明（opt-out），而非默认继承

#### 实现机制

`tests/gateway/conftest.py` 中的 `auto_reset_gateway_state` fixture（autouse=True）：

```python
@pytest.fixture(autouse=True)
def auto_reset_gateway_state(request):
    """
    自动重置 Gateway 状态的 fixture（autouse=True）
    
    排除规则:
    - 标记了 @pytest.mark.no_singleton_reset 的测试不会执行 reset
    """
    skip_reset = request.node.get_closest_marker("no_singleton_reset") is not None
    
    if not skip_reset:
        reset_all_gateway_state()  # setup
    
    yield
    
    if not skip_reset:
        reset_all_gateway_state()  # teardown
```

#### 统一重置入口

`engram.gateway.testing.reset_gateway_runtime_state()` 是所有状态重置的统一入口：

```python
# 重置内容:
# 1. 纯单例: config, logbook_adapter, openmemory_client, container
# 2. ContextVar: _current_correlation_id, _request_correlation_id
# 3. 全局注册器: _tool_executor
```

### 环境变量清洗

除了状态重置，`auto_cleanup_gateway_env_vars` fixture 还会清洗以下环境变量：

- `UNKNOWN_ACTOR_POLICY`
- `VALIDATE_EVIDENCE_REFS`
- `STRICT_MODE_ENFORCE_VALIDATE_REFS`
- `PROJECT_KEY`
- `POSTGRES_DSN`
- `OPENMEMORY_BASE_URL`

排除方式：`@pytest.mark.no_env_cleanup` 或 `@pytest.mark.no_singleton_reset`

---

## Opt-out Marker 使用边界

### 核心规则

```
┌─────────────────────────────────────────────────────────────────┐
│              @pytest.mark.no_singleton_reset 使用边界            │
├─────────────────────────────────────────────────────────────────┤
│  ✅ 允许：仅限集成测试 / E2E 测试                                │
│  ❌ 禁止：单元测试中使用                                         │
└─────────────────────────────────────────────────────────────────┘
```

### 详细规则

| 测试类型 | 允许使用 no_singleton_reset | 原因 |
|----------|----------------------------|------|
| **单元测试** | ❌ 禁止 | 单元测试应完全隔离，不依赖外部状态 |
| **集成测试** (带 `@pytest.mark.integration`) | ✅ 允许 | 需要复用连接池、共享数据库状态 |
| **E2E 测试** (带 `@pytest.mark.gate_profile("full")`) | ✅ 允许 | 需要测试完整链路，包括真实依赖 |
| **性能测试** | ✅ 允许 | 避免 reset 开销影响测量结果 |

### 合规使用示例

```python
# ✅ 正确: 集成测试 + gate_profile("full")
@pytest.mark.gate_profile("full")
@pytest.mark.integration
@pytest.mark.no_singleton_reset
def test_e2e_with_shared_connection():
    """需要复用数据库连接池的 E2E 测试"""
    pass

# ✅ 正确: 模块级 pytestmark (整个模块都是集成测试)
# 在模块顶部声明:
# pytestmark = [pytest.mark.gate_profile("full"), pytest.mark.integration]
@pytest.mark.no_singleton_reset
def test_integration_scenario():
    """集成测试场景"""
    pass

# ✅ 正确: 跨测试共享数据库状态
@pytest.mark.integration
@pytest.mark.no_singleton_reset
class TestDatabaseStateSharing:
    """需要跨测试共享数据库状态的测试类"""
    
    def test_create_data(self, db_conn):
        # 创建数据
        pass
    
    def test_read_data(self, db_conn):
        # 读取上一个测试创建的数据
        pass
```

### 违规使用示例

```python
# ❌ 错误: 单元测试使用 no_singleton_reset
@pytest.mark.no_singleton_reset
def test_unit_with_opt_out():
    """单元测试不应使用 opt-out marker"""
    pass  # 会被 CI 回归测试拦截

# ❌ 错误: 没有标记 integration 或 gate_profile
@pytest.mark.no_singleton_reset
def test_without_integration_marker():
    """缺少 integration/gate_profile marker"""
    pass  # 会被 CI 回归测试拦截
```

### 回归测试机制

`tests/gateway/test_opt_out_policy_contract.py` 扫描所有 `no_singleton_reset` 使用：
- 确保只出现在合规的集成/E2E 测试中
- 单元测试使用会导致 CI 失败

### Opt-out 场景汇总

| 场景 | 需要 opt-out 的原因 | 必须的标记 |
|------|---------------------|-----------|
| E2E 集成测试 | 需要复用连接池，避免频繁重建 | `@pytest.mark.gate_profile("full")` |
| 跨测试共享数据库状态 | 测试间需要看到彼此的数据变更 | `@pytest.mark.integration` |
| 性能测试 | 避免 reset 开销影响测量结果 | `@pytest.mark.integration` |
| Worker 集成测试 | outbox_worker 使用模块级 adapter | `@pytest.mark.integration` |

---

## Fixture 使用指南

### 单元测试（推荐）

使用 `gateway_deps` fixture 进行依赖注入：

```python
@pytest.mark.asyncio
async def test_memory_store(gateway_deps, test_correlation_id):
    result = await memory_store_impl(
        payload_md="test content",
        correlation_id=test_correlation_id,
        deps=gateway_deps,
    )
    assert result["ok"] is True
```

### 自定义依赖

使用 `GatewayDeps.for_testing()` 注入自定义依赖：

```python
@pytest.mark.asyncio
async def test_with_custom_adapter(fake_openmemory_client, test_correlation_id):
    from tests.gateway.fakes import FakeLogbookAdapter
    from engram.gateway.di import GatewayDeps
    
    adapter = FakeLogbookAdapter()
    adapter.configure_dedup_hit(memory_id="existing_memory")
    
    deps = GatewayDeps.for_testing(
        logbook_adapter=adapter,
        openmemory_client=fake_openmemory_client,
    )
    
    result = await memory_store_impl(
        payload_md="test",
        correlation_id=test_correlation_id,
        deps=deps,
    )
```

### FastAPI 集成测试

使用 `gateway_test_container` fixture：

```python
def test_mcp_endpoint(gateway_test_container):
    from engram.gateway.main import app
    from fastapi.testclient import TestClient
    
    # app 将使用 gateway_test_container 中的 fake 依赖
    with TestClient(app) as client:
        response = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1,
        })
    assert response.status_code == 200
```

### ContextVar 测试

测试 correlation_id 传递时：

```python
@pytest.mark.asyncio
async def test_correlation_id_propagation():
    from engram.gateway.mcp_rpc import (
        set_current_correlation_id,
        get_current_correlation_id,
        reset_current_correlation_id_for_testing,
    )
    
    try:
        token = set_current_correlation_id("corr-test1234567890ab")
        assert get_current_correlation_id() == "corr-test1234567890ab"
    finally:
        # 显式清理（auto_reset_gateway_state 也会处理）
        reset_current_correlation_id_for_testing()
```

---

## sys.modules 注入测试

### 使用场景

测试模块导入失败场景（如可选依赖缺失）时，需要操作 `sys.modules`。

### SysModulesPatcher 工具类

`tests/gateway/conftest.py` 提供 `SysModulesPatcher` 和 `sys_modules_patcher` fixture：

```python
@pytest.mark.asyncio
async def test_import_failure(sys_modules_patcher):
    """测试可选依赖缺失时的降级行为"""
    # 需要管理的模块列表
    modules = [
        "engram.gateway.evidence_store",
        "engram.gateway.handlers.evidence_upload",
    ]
    
    # 创建 patcher（自动保存原始模块状态）
    patcher = sys_modules_patcher(modules)
    
    # 注入失败的模块
    patcher.inject_failing_import(
        "engram.gateway.evidence_store",
        "No module named 'engram_logbook' (mocked)"
    )
    
    # 清除 handler 缓存以触发重新导入
    patcher.remove_module("engram.gateway.handlers.evidence_upload")
    
    # 测试代码...
    from engram.gateway.handlers.evidence_upload import execute_evidence_upload
    
    result = await execute_evidence_upload(
        content="test",
        content_type="text/plain",
        title="test.txt",
        correlation_id="corr-test1234567890ab",
    )
    
    # 验证降级行为
    assert result["ok"] is False
    assert result["error_code"] == "DEPENDENCY_MISSING"
    
    # 无需手动清理，fixture 会自动恢复
```

### SysModulesPatcher API

| 方法 | 说明 |
|------|------|
| `inject_failing_import(module_name, error_message)` | 注入一个访问时抛出 ImportError 的 mock 模块 |
| `remove_module(module_name)` | 从 sys.modules 移除指定模块（触发重新导入） |
| `restore()` | 恢复所有原始模块状态（fixture 自动调用） |

### 注意事项

1. **仅用于测试可选依赖缺失场景**，不要用于普通单元测试
2. **fixture 自动清理**，无需手动调用 `restore()`
3. **配合 `remove_module()` 使用**，确保 handler 缓存被清除后重新导入

---

## 状态生命周期图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Gateway 状态生命周期                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  进程启动                                                                │
│     │                                                                   │
│     ▼                                                                   │
│  ┌─────────────────┐                                                    │
│  │ 模块导入        │  → globals 缓存初始化 (engram.gateway.__getattr__)  │
│  └────────┬────────┘                                                    │
│           │                                                             │
│           ▼                                                             │
│  ┌─────────────────┐                                                    │
│  │ app 启动        │  → register_tool_executor()                        │
│  │ (lifespan)      │  → get_container() → 延迟初始化单例                 │
│  └────────┬────────┘                                                    │
│           │                                                             │
│           ▼                                                             │
│  ┌─────────────────┐                                                    │
│  │ 请求处理        │  → CorrelationIdMiddleware 设置 ContextVar          │
│  │                 │  → dispatch() 设置 _current_correlation_id         │
│  │                 │  → handler 访问 deps → 使用已初始化的单例            │
│  └────────┬────────┘                                                    │
│           │                                                             │
│           ▼                                                             │
│  ┌─────────────────┐                                                    │
│  │ 请求结束        │  → ContextVar reset(token) 恢复                     │
│  └────────┬────────┘                                                    │
│           │                                                             │
│   (测试场景)                                                             │
│           ▼                                                             │
│  ┌─────────────────┐                                                    │
│  │ 测试 teardown   │  → reset_gateway_runtime_state()                   │
│  │                 │     ├── reset_all_singletons()                     │
│  │                 │     ├── reset_current_correlation_id_for_testing() │
│  │                 │     └── reset_request_correlation_id_for_testing() │
│  └─────────────────┘                                                    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 相关文档

- [Gateway DI 与入口边界统一](adr_gateway_di_and_entry_boundary.md)
- [Gateway 模块边界](gateway_module_boundaries.md)
- [CI 门禁 Runbook](../dev/ci_gate_runbook.md)
- [Agent 协作指南](../../AGENTS.md)

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-02-01 | 重大更新：详细梳理所有状态持有点，添加 reset API 层级图、推荐测试写法示例、opt-out marker 使用边界说明、sys.modules 注入测试文档 |
| 2026-02-01 | 初始版本：梳理 7 个模块级状态变量，定义测试隔离规则 |
