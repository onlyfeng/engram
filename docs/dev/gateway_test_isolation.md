# Gateway 测试隔离指南

本文档定义 Gateway 测试的隔离策略、依赖注入规范、opt-out 机制和常见陷阱。

> **单一事实来源**：测试隔离的 fixtures 实现位于 `tests/gateway/conftest.py`

---

## 目录

- [核心原则](#核心原则)
- [单元测试：严格依赖注入](#单元测试严格依赖注入)
- [集成测试：Gate Profile 系统](#集成测试gate-profile-系统)
- [Opt-out 机制](#opt-out-机制)
- [常见陷阱](#常见陷阱)
- [Fake 依赖体系](#fake-依赖体系)
- [测试 Fixture 一览](#测试-fixture-一览)

---

## 核心原则

Gateway 测试遵循以下核心原则：

| 原则 | 说明 | 实现 |
|------|------|------|
| **测试隔离** | 每个测试用例独立，无状态泄漏 | `auto_reset_gateway_state` fixture |
| **环境无污染** | 测试不依赖/不污染真实环境变量 | `auto_cleanup_gateway_env_vars` fixture |
| **显式依赖注入** | 被测代码的依赖必须显式注入 | `GatewayDeps.for_testing()` 严格模式 |
| **可预测行为** | Fake 依赖行为可配置、可断言 | `FakeOpenMemoryClient`, `FakeLogbookAdapter` |

---

## 单元测试：严格依赖注入

### 禁止行为

单元测试**禁止**：

1. **触发全局容器** - 不要使用 `get_container()` 或 `set_container()`
2. **依赖真实环境变量** - 不要读取 `POSTGRES_DSN`, `OPENMEMORY_BASE_URL` 等
3. **使用 module-level 单例** - 不要直接导入 `logbook_adapter` 模块
4. **使用 `no_singleton_reset` marker** - 单元测试禁止 opt-out

### 正确做法：`GatewayDeps.for_testing()`

所有 handler 单元测试必须使用 `GatewayDeps.for_testing()` 进行依赖注入：

```python
import pytest
from engram.gateway.di import GatewayDeps
from engram.gateway.handlers.memory_store import memory_store_impl
from tests.gateway.fakes import (
    FakeGatewayConfig,
    FakeLogbookAdapter,
    FakeOpenMemoryClient,
)

@pytest.mark.asyncio
async def test_memory_store_success(test_correlation_id):
    """正确示例：显式注入所有依赖"""
    # 1. 创建 fake 依赖
    fake_config = FakeGatewayConfig()
    fake_adapter = FakeLogbookAdapter()
    fake_client = FakeOpenMemoryClient()

    # 2. 配置 fake 行为
    fake_adapter.configure_settings(team_write_enabled=True)
    fake_adapter.configure_dedup_miss()
    fake_client.configure_store_success(memory_id="mem_123")

    # 3. 通过 GatewayDeps.for_testing() 注入
    deps = GatewayDeps.for_testing(
        config=fake_config,
        logbook_adapter=fake_adapter,
        openmemory_client=fake_client,
    )

    # 4. 调用被测函数
    result = await memory_store_impl(
        payload_md="测试内容",
        correlation_id=test_correlation_id,
        deps=deps,
    )

    # 5. 断言结果
    assert result["ok"] is True
    assert result["action"] == "allow"
```

### 严格模式

`GatewayDeps.for_testing()` 默认启用严格模式（`_testing_strict=True`）：

```python
# 如果 handler 使用了 deps.openmemory_client，但未注入：
deps = GatewayDeps.for_testing(
    config=fake_config,
    logbook_adapter=fake_adapter,
    # 缺少 openmemory_client!
)

# 被测代码访问 deps.openmemory_client 时会抛出 RuntimeError：
#   RuntimeError: [GatewayDeps 严格模式] openmemory_client 未注入...
```

**严格模式确保**：
- 测试不会意外连接真实数据库
- 测试不会意外调用真实 OpenMemory 服务
- 被测代码的依赖关系清晰可见

### 使用 `gateway_deps` fixture（推荐）

对于标准的 handler 测试，可直接使用 `gateway_deps` fixture：

```python
@pytest.mark.asyncio
async def test_memory_store_with_fixture(gateway_deps, test_correlation_id):
    """使用 gateway_deps fixture 的简化示例"""
    result = await memory_store_impl(
        payload_md="测试内容",
        correlation_id=test_correlation_id,
        deps=gateway_deps,
    )
    assert result["ok"] is True
```

`gateway_deps` fixture 已预配置：
- `FakeGatewayConfig()`
- `FakeLogbookAdapter()` (configured with `dedup_miss`)
- `FakeOpenMemoryClient()` (configured with `store_success`)

---

## 集成测试：Gate Profile 系统

### Gate Profile 概念

集成测试使用 Gate Profile 控制测试范围：

| Profile | 环境变量 | 说明 | 适用场景 |
|---------|----------|------|----------|
| `http_only` | `HTTP_ONLY_MODE=1` | 纯 HTTP 验证，跳过 Docker/MCP | CI 推荐 |
| `standard` | `SKIP_DEGRADATION_TEST=1` | HTTP + JSON-RPC，无降级测试 | 本地开发 |
| `full` | `SKIP_DEGRADATION_TEST=0` | 完整模式，含降级测试 | 需要 Docker 权限 |

### 使用 `--gate-profile` CLI 选项

```bash
# CI 推荐：纯 HTTP 验证
pytest tests/gateway/ -v --gate-profile http_only

# 本地开发：标准模式
pytest tests/gateway/ -v --gate-profile standard

# 完整集成测试（需要 Docker 权限和数据库）
pytest tests/gateway/ -v --gate-profile full
```

### 使用 `@pytest.mark.gate_profile` marker

在测试函数上声明所需的最低 profile：

```python
import pytest

@pytest.mark.gate_profile("full")
@pytest.mark.integration
def test_degradation_flow():
    """此测试只在 full profile 下运行"""
    # 需要真实的 Docker 环境和数据库
    pass

@pytest.mark.gate_profile("standard")
def test_jsonrpc_protocol():
    """此测试需要 standard 或 full profile"""
    pass
```

**Profile 优先级**：`http_only (1) < standard (2) < full (3)`

- 如果当前 profile 低于测试要求的 profile，测试会被 **skip**
- 如果 full profile 缺少必要能力（Docker/DB），可能会 **fail**

### Makefile 目标

```bash
# CI 推荐
make test-gateway-integration  # http_only profile

# 完整集成测试
make test-gateway-integration-full  # full profile
```

---

## Opt-out 机制

### `@pytest.mark.no_singleton_reset`

**作用**：跳过测试前后的 Gateway 单例重置

**使用门槛**：

1. **单元测试禁止使用** - 会被回归测试拦截
2. **必须同时标记集成测试** - 需要 `@pytest.mark.integration` 或 `@pytest.mark.gate_profile("full")`

```python
# 正确：集成测试 + gate_profile
@pytest.mark.gate_profile("full")
@pytest.mark.integration
@pytest.mark.no_singleton_reset
def test_e2e_with_shared_connection():
    """此测试复用连接池，不重置单例"""
    pass

# 错误：单元测试使用 no_singleton_reset（CI 会失败）
@pytest.mark.no_singleton_reset
def test_unit_should_not_use_opt_out():  # ❌ 违规
    pass
```

**回归测试**：`tests/gateway/test_opt_out_policy_contract.py` 会扫描所有使用，确保合规。

### `@pytest.mark.no_env_cleanup`

**作用**：跳过测试前的环境变量清洗

**清洗的环境变量**：
- `UNKNOWN_ACTOR_POLICY`
- `VALIDATE_EVIDENCE_REFS`
- `STRICT_MODE_ENFORCE_VALIDATE_REFS`
- `PROJECT_KEY`
- `POSTGRES_DSN`
- `OPENMEMORY_BASE_URL`

**使用门槛**：仅用于需要保留真实环境变量的集成/E2E 测试

```python
@pytest.mark.no_env_cleanup
@pytest.mark.gate_profile("full")
def test_integration_with_real_env():
    """此测试依赖真实环境变量，不清洗"""
    pass
```

**注意**：标记 `no_singleton_reset` 会自动跳过环境变量清洗。

---

## 常见陷阱

### 1. ContextVars Token 未 Reset

**问题**：`correlation_id` 等 ContextVar 在测试间泄漏

**涉及的 ContextVars**：
- `mcp_rpc._current_correlation_id` - dispatch 上下文中的 correlation_id
- `middleware._request_correlation_id` - HTTP 请求上下文中的 correlation_id

**解决方案**：`auto_reset_gateway_state` fixture 会调用 `reset_gateway_runtime_state()`，重置所有 ContextVar：

```python
# src/engram/gateway/testing/reset.py
def reset_gateway_runtime_state():
    # 重置 mcp_rpc ContextVar
    reset_current_correlation_id_for_testing()
    # 重置 middleware ContextVar
    reset_request_correlation_id_for_testing()
```

**如果你使用了 `no_singleton_reset`**：需要手动在测试后重置：

```python
@pytest.mark.no_singleton_reset
@pytest.mark.gate_profile("full")
def test_with_manual_cleanup():
    try:
        # 测试代码
        pass
    finally:
        from engram.gateway.mcp_rpc import reset_current_correlation_id_for_testing
        reset_current_correlation_id_for_testing()
```

### 2. logbook_adapter 构造会写入 `POSTGRES_DSN` 环境变量

**问题**：`FakeLogbookAdapter` 或真实 `LogbookAdapter` 的构造可能读取/写入 `POSTGRES_DSN`

**影响**：
- 如果测试前未清理 `POSTGRES_DSN`，可能连接到错误的数据库
- 如果测试设置了 `POSTGRES_DSN`，可能污染后续测试

**解决方案**：

1. **单元测试**：使用 `GatewayDeps.for_testing()` + `FakeLogbookAdapter`，完全绕过环境变量
2. **集成测试**：使用 `logbook_adapter_config` fixture

```python
# 单元测试：完全不依赖环境变量
@pytest.mark.asyncio
async def test_unit(gateway_deps, test_correlation_id):
    # gateway_deps 使用 FakeLogbookAdapter，不读取 POSTGRES_DSN
    result = await memory_store_impl(deps=gateway_deps, ...)

# 集成测试：使用 fixture 管理环境变量
def test_integration(logbook_adapter_config, migrated_db):
    # logbook_adapter_config 会：
    # 1. 设置 POSTGRES_DSN 为测试数据库
    # 2. 测试后恢复原始值
    pass
```

### 3. mcp_rpc `_tool_executor` 注册表的生命周期

**问题**：`_tool_executor` 是模块级全局变量，在 `register_tool_executor()` 调用后持续存在

**影响**：
- 测试 A 注册了自定义 executor，测试 B 可能使用了测试 A 的 executor
- FastAPI app 启动时会调用 `register_tool_executor()`，如果测试复用 app 实例，executor 状态会污染

**解决方案**：

1. `auto_reset_gateway_state` fixture 会调用 `reset_tool_executor_for_testing()`
2. 如果需要测试自定义 executor，使用 fixture 确保清理：

```python
import pytest
from engram.gateway.mcp_rpc import (
    register_tool_executor,
    reset_tool_executor_for_testing,
)

@pytest.fixture
def custom_executor():
    """注册自定义 executor 并在测试后清理"""
    my_executor = ...
    register_tool_executor(my_executor)
    yield my_executor
    reset_tool_executor_for_testing()

def test_with_custom_executor(custom_executor):
    # 使用自定义 executor
    pass
```

### 4. FastAPI TestClient 与全局容器

**问题**：使用 `TestClient(app)` 时，FastAPI 的依赖注入会读取全局容器

**解决方案**：使用 `gateway_test_container` fixture：

```python
def test_mcp_endpoint(gateway_test_container):
    """使用 gateway_test_container 设置全局容器"""
    from engram.gateway.main import app
    from fastapi.testclient import TestClient

    # gateway_test_container 已设置全局容器为 fake 依赖
    with TestClient(app) as client:
        response = client.post("/mcp", json={...})

    # 测试结束后，auto_reset_gateway_state 会重置全局容器
```

---

## Fake 依赖体系

### 类层次

```
tests/gateway/fakes.py
├── FakeGatewayConfig          # 配置对象（dataclass）
├── FakeOpenMemoryClient       # 实现 OpenMemoryPort 协议
├── FakeLogbookAdapter         # 实现 WriteAuditPort 协议
├── FakeLogbookDatabase        # [已弃用] v2.0 将移除
├── FakeToolRegistry           # 工具注册表（用于 MCP 测试）
└── FakeToolExecutor           # 工具执行器（用于 MCP 测试）
```

### FakeOpenMemoryClient

可配置的 OpenMemory 客户端 fake：

```python
from tests.gateway.fakes import FakeOpenMemoryClient

client = FakeOpenMemoryClient()

# 配置成功
client.configure_store_success(memory_id="mem_123")
client.configure_search_success(results=[...])

# 配置失败
client.configure_store_connection_error("连接超时")
client.configure_store_api_error(status_code=500)
client.configure_store_generic_error("未知错误")

# 获取调用记录
last_call = client.get_last_store_call()
all_calls = client.store_calls
```

### FakeLogbookAdapter

可配置的 Logbook adapter fake：

```python
from tests.gateway.fakes import FakeLogbookAdapter

adapter = FakeLogbookAdapter()

# 配置 settings
adapter.configure_settings(team_write_enabled=True, policy_json={})

# 配置 dedup
adapter.configure_dedup_hit(outbox_id=100, memory_id="mem_existing")
adapter.configure_dedup_miss()

# 配置 audit
adapter.configure_audit_success(start_id=1)
adapter.configure_audit_failure(error="审计写入失败")

# 配置 outbox
adapter.configure_outbox_success(start_id=1)
adapter.configure_outbox_failure(error="入队失败")

# 获取调用记录
audit_calls = adapter.get_audit_calls()
outbox_calls = adapter.get_outbox_calls()
```

### FakeLogbookDatabase（已弃用）

> **警告**：`FakeLogbookDatabase` 已弃用，将在 v2.0 移除。
> 新测试必须使用 `FakeLogbookAdapter`。

迁移指南：

```python
# 旧代码（已弃用）
from tests.gateway.fakes import FakeLogbookDatabase
db = FakeLogbookDatabase()
deps = GatewayDeps.for_testing(db=db)  # ❌ db 参数已移除

# 新代码（推荐）
from tests.gateway.fakes import FakeLogbookAdapter
adapter = FakeLogbookAdapter()
deps = GatewayDeps.for_testing(logbook_adapter=adapter)  # ✅
```

---

## 测试 Fixture 一览

### 自动应用 Fixtures（autouse=True）

| Fixture | Scope | 作用 | 排除 Marker |
|---------|-------|------|-------------|
| `auto_reset_gateway_state` | function | 重置所有 Gateway 运行时状态 | `no_singleton_reset` |
| `auto_cleanup_gateway_env_vars` | function | 清洗 Gateway 相关环境变量 | `no_env_cleanup`, `no_singleton_reset` |
| `gate_profile_env_setup` | session | 根据 `--gate-profile` 设置环境变量 | - |

### 依赖注入 Fixtures

| Fixture | Scope | 返回类型 | 说明 |
|---------|-------|----------|------|
| `fake_gateway_config` | function | `FakeGatewayConfig` | Fake 配置 |
| `fake_logbook_adapter` | function | `FakeLogbookAdapter` | Fake adapter |
| `fake_openmemory_client` | function | `FakeOpenMemoryClient` | Fake client |
| `gateway_deps` | function | `GatewayDeps` | 完整的依赖容器（严格模式） |
| `test_correlation_id` | function | `str` | 测试用 correlation_id |

### 数据库 Fixtures

| Fixture | Scope | 说明 |
|---------|-------|------|
| `test_db_info` | session | 创建独立测试数据库 |
| `migrated_db` | session | 执行迁移后的数据库 |
| `db_conn` | function | 自动回滚的连接 |
| `db_conn_committed` | function | 可提交的连接 |

### FastAPI 集成 Fixtures

| Fixture | Scope | 说明 |
|---------|-------|------|
| `gateway_test_container` | function | 设置全局容器为 fake 依赖 |
| `logbook_adapter_config` | function | 配置 adapter 使用测试数据库 |

---

## 相关文档

| 主题 | 文档路径 |
|------|----------|
| Gateway 能力边界 | [docs/gateway/07_capability_boundary.md](../gateway/07_capability_boundary.md) |
| 两阶段审计 DB 隔离 | [docs/dev/two_phase_audit_e2e_db_isolation.md](two_phase_audit_e2e_db_isolation.md) |
| v1.0 升级指南 | [docs/gateway/upgrade_v1_0_remove_handler_di_compat.md](../gateway/upgrade_v1_0_remove_handler_di_compat.md) |
| 弃用 logbook_db 参考 | [docs/architecture/deprecated_logbook_db_references_ssot.md](../architecture/deprecated_logbook_db_references_ssot.md) |

---

**文档版本**：v1.0  
**最后更新**：2026-02-01
