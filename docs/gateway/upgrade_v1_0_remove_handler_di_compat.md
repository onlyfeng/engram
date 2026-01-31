# v1.0 升级指南：移除 Handler DI 兼容层

> 状态: **计划中**  
> 创建日期: 2026-01-31  
> 目标版本: v1.0  
> 前置版本: v0.9.x

---

## 1. 概述

本文档描述从 v0.9 升级到 v1.0 时的 **Handler 依赖注入（DI）兼容层移除**变更。

### 1.1 变更摘要

| 维度 | v0.9（当前） | v1.0（目标） |
|------|-------------|-------------|
| `deps` 参数 | `Optional[GatewayDeps]`（可选） | `GatewayDeps`（必需） |
| Legacy 参数 | `_config`, `_db`, `_openmemory_client` 产生 `DeprecationWarning` | **已移除** |
| handlers 内部 DI | 允许 `GatewayDeps.create()` 回退 | **禁止**，必须由入口层传入 |
| 对外契约 | HTTP/MCP/JSON-RPC 响应格式 | **不变** |

### 1.2 设计文档参考

- [ADR: Gateway DI 与入口边界统一](../architecture/adr_gateway_di_and_entry_boundary.md)
- [Gateway 设计](./06_gateway_design.md)
- [Gateway 能力边界](./07_capability_boundary.md)

---

## 2. 受影响的 API

### 2.1 handlers 层函数

| Handler | 模块路径 | 变更类型 |
|---------|----------|----------|
| `memory_store_impl` | `handlers/memory_store.py` | 签名变更 + Legacy 参数移除 |
| `memory_query_impl` | `handlers/memory_query.py` | 签名变更 + Legacy 参数移除 |
| `governance_update_impl` | `handlers/governance_update.py` | 签名变更 + Legacy 参数移除 |
| `execute_evidence_upload` | `handlers/evidence_upload.py` | 签名变更 + Legacy 参数移除 |
| `get_dependencies` | `handlers/__init__.py` | **移除**（v1.0 禁止 handlers 自行获取依赖） |

### 2.2 入口层函数

| 函数 | 模块路径 | 变更类型 |
|------|----------|----------|
| `_validate_actor_user` | `main.py` | **考虑移除**（逻辑合并到 handler 内部） |

### 2.3 模块级全局函数（handlers 内禁止使用）

| 函数 | 模块路径 | v0.9 状态 | v1.0 状态 |
|------|----------|-----------|-----------|
| `get_config()` | `config.py` | 不推荐 | **handlers 内禁止**（仅入口层可用） |
| `get_db()` | `logbook_db.py` | 弃用 | **模块移除** |
| `get_adapter()` | `logbook_adapter.py` | 不推荐 | **handlers 内禁止**（仅入口层可用） |
| `get_client()` | `openmemory_client.py` | 不推荐 | **handlers 内禁止**（仅入口层可用） |
| `get_container()` | `container.py` | 仅限入口层 | 保持（handlers 禁止调用） |
| `GatewayDeps.create()` | `di.py` | handlers 内回退 | **handlers 内禁止** |

---

## 3. 签名变更详情

### 3.1 memory_store_impl

**v0.9（当前）**：

```python
async def memory_store_impl(
    payload_md: str,
    target_space: Optional[str] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    kind: Optional[str] = None,
    evidence_refs: Optional[List[str]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    is_bulk: bool = False,
    item_id: Optional[int] = None,
    actor_user_id: Optional[str] = None,
    correlation_id: Optional[str] = None,  # v0.9: 入口层传入
    deps: Optional[GatewayDeps] = None,    # v0.9: 可选，回退到 GatewayDeps.create()
    # Legacy 参数（v0.9 弃用，产生 DeprecationWarning）
    _config: Optional[GatewayConfig] = None,
    _db: Optional[LogbookDatabase] = None,
    _openmemory_client: Optional[OpenMemoryClient] = None,
) -> MemoryStoreResponse:
    # 兼容处理
    if deps is None:
        if _config is not None or _db is not None or _openmemory_client is not None:
            warnings.warn("Legacy _config/_db/_openmemory_client 参数已弃用，请使用 deps 参数", DeprecationWarning)
        deps = GatewayDeps.create()  # v0.9 允许回退
    ...
```

**v1.0（目标）**：

```python
async def memory_store_impl(
    payload_md: str,
    target_space: Optional[str] = None,
    meta_json: Optional[Dict[str, Any]] = None,
    kind: Optional[str] = None,
    evidence_refs: Optional[List[str]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    is_bulk: bool = False,
    item_id: Optional[int] = None,
    actor_user_id: Optional[str] = None,
    correlation_id: str,         # v1.0: 必需（入口层传入）
    deps: GatewayDeps,           # v1.0: 必需（入口层传入）
    # Legacy 参数已移除
) -> MemoryStoreResponse:
    # 无兼容处理，直接使用 deps
    config = deps.config
    adapter = deps.logbook_adapter
    client = deps.openmemory_client
    ...
```

### 3.2 memory_query_impl

**v0.9（当前）**：

```python
async def memory_query_impl(
    query: str,
    spaces: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
    top_k: int = 10,
    correlation_id: Optional[str] = None,
    deps: Optional[GatewayDeps] = None,
    # Legacy 参数
    _config: Optional[GatewayConfig] = None,
    _openmemory_client: Optional[OpenMemoryClient] = None,
) -> MemoryQueryResponse:
    ...
```

**v1.0（目标）**：

```python
async def memory_query_impl(
    query: str,
    spaces: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
    top_k: int = 10,
    correlation_id: str,         # v1.0: 必需
    deps: GatewayDeps,           # v1.0: 必需
) -> MemoryQueryResponse:
    ...
```

### 3.3 governance_update_impl

**v0.9（当前）**：

```python
async def governance_update_impl(
    team_write_enabled: Optional[bool] = None,
    policy_json: Optional[Dict[str, Any]] = None,
    admin_key: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    deps: Optional[GatewayDeps] = None,
    # Legacy 参数
    _config: Optional[GatewayConfig] = None,
    _db: Optional[LogbookDatabase] = None,
) -> GovernanceUpdateResponse:
    ...
```

**v1.0（目标）**：

```python
async def governance_update_impl(
    team_write_enabled: Optional[bool] = None,
    policy_json: Optional[Dict[str, Any]] = None,
    admin_key: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    correlation_id: str,         # v1.0: 必需
    deps: GatewayDeps,           # v1.0: 必需
) -> GovernanceUpdateResponse:
    ...
```

### 3.4 execute_evidence_upload

**v0.9（当前）**：

```python
async def execute_evidence_upload(
    content: str,
    content_type: str,
    title: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    project_key: Optional[str] = None,
    item_id: Optional[int] = None,
    correlation_id: Optional[str] = None,
    deps: Optional[GatewayDeps] = None,
) -> EvidenceUploadResponse:
    ...
```

**v1.0（目标）**：

```python
async def execute_evidence_upload(
    content: str,
    content_type: str,
    title: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    project_key: Optional[str] = None,
    item_id: Optional[int] = None,
    correlation_id: str,         # v1.0: 必需
    deps: GatewayDeps,           # v1.0: 必需
) -> EvidenceUploadResponse:
    ...
```

### 3.5 handlers.get_dependencies（移除）

**v0.9（当前）**：

```python
# handlers/__init__.py
def get_dependencies() -> GatewayDeps:
    """获取全局依赖（handlers 内部回退用）"""
    return GatewayDeps.create()
```

**v1.0（目标）**：

```python
# handlers/__init__.py
# get_dependencies() 已移除
# handlers 必须通过 deps 参数接收依赖
```

### 3.6 main._validate_actor_user（考虑移除）

**v0.9（当前）**：

```python
# main.py
def _validate_actor_user(actor_user_id: Optional[str], config: GatewayConfig) -> Optional[str]:
    """验证 actor_user_id（入口层调用）"""
    if actor_user_id and config.validate_actor_user:
        # 校验逻辑
        ...
    return actor_user_id
```

**v1.0 方案 A（保留但简化）**：

```python
# 入口层保留，但依赖从 deps 获取
def _validate_actor_user(actor_user_id: Optional[str], deps: GatewayDeps) -> Optional[str]:
    ...
```

**v1.0 方案 B（合并到 handler）**：

```python
# 移除独立函数，逻辑合并到各 handler 内部
# 由 handler 在处理业务前自行校验
```

---

## 4. 迁移时间表

### 4.1 版本窗口

| 版本 | 状态 | 说明 |
|------|------|------|
| **v0.9.x（当前）** | 兼容期 | Legacy 参数可用，产生 `DeprecationWarning`；`deps` 可选 |
| **v1.0（目标）** | 移除期 | Legacy 参数移除；`deps` 必需；handlers 内禁止 `GatewayDeps.create()` |

### 4.2 迁移检查清单

#### 应用代码迁移

- [ ] 将所有 handler 调用从 legacy 参数改为 `deps` 参数
- [ ] 移除 handlers 中对 `get_config()`、`get_adapter()`、`get_client()` 的直接调用
- [ ] 移除 handlers 中对 `GatewayDeps.create()` 的回退调用
- [ ] 确保入口层（`app.py`、`main.py`）统一调用 `get_gateway_deps()` 并传递给 handler

#### 测试代码迁移

- [ ] 更新所有测试使用 `GatewayDeps.for_testing()` 注入 mock
- [ ] 移除测试中对 legacy 参数的使用
- [ ] 验证契约测试继续通过（见第 5 节）

#### 代码清理

- [ ] 移除 `logbook_db.py` 模块（完全由 `logbook_adapter.py` 替代）
- [ ] 移除 handler 签名中的 legacy 参数
- [ ] 将 `deps` 和 `correlation_id` 参数从 `Optional` 改为必需
- [ ] 添加 lint 规则禁止 handlers 导入隐式依赖（可选）

---

## 5. 对外契约不变性保证

### 5.1 不变边界声明

本次升级 **不改变** 以下对外契约：

| 契约类型 | 具体内容 | 验证方式 |
|----------|----------|----------|
| **HTTP 响应格式** | `/mcp`、`/memory/store`、`/memory/query` 等端点的请求/响应 Schema | 集成测试 |
| **MCP 协议** | JSON-RPC 2.0 请求/响应格式、tools/list、tools/call | 契约测试 |
| **错误码** | ErrorData 结构、JsonRpcErrorCode 枚举、ErrorCategory/ErrorReason | 契约测试 |
| **审计事件** | `audit_event_v1.schema.json` 定义的字段和结构 | Schema 验证 |
| **Outbox 状态机** | pending/sent/dead 状态转换、审计 reason 映射 | 不变量测试 |

### 5.2 验收测试引用

以下测试在 v1.0 升级后 **必须继续通过**：

| 测试文件 | 覆盖契约 | 说明 |
|----------|----------|------|
| `test_mcp_jsonrpc_contract.py` | JSON-RPC 协议契约 | 请求解析、错误码、ErrorData 结构 |
| `test_audit_event_contract.py` | 审计事件结构契约 | Schema 验证、evidence_refs_json 查询 |
| `test_error_codes.py` | 错误码与 ErrorData 契约 | category/reason/retryable/correlation_id |
| `test_unified_stack_integration.py` | 端到端集成契约 | 完整业务流程 |
| `test_reconcile_outbox.py` | Audit ↔ Outbox 闭环契约 | 状态映射一致性 |
| `test_reliability_report_contract.py` | 可靠性报告结构契约 | Schema 验证 |

### 5.3 CI 验收命令

```bash
# 运行所有契约测试
make test-gateway-contracts

# 或单独运行
pytest tests/gateway/test_mcp_jsonrpc_contract.py -v
pytest tests/gateway/test_audit_event_contract.py -v
pytest tests/gateway/test_error_codes.py -v
pytest tests/gateway/test_unified_stack_integration.py -v
pytest tests/gateway/test_reconcile_outbox.py -v
```

---

## 6. 回滚策略

### 6.1 回滚触发条件

- 升级后契约测试失败
- 生产环境出现依赖注入相关错误（如 `deps is None`）
- 审计记录丢失或格式异常

### 6.2 回滚步骤

**容器化部署**：

```bash
# 回滚到 v0.9.x 镜像
docker pull <registry>/engram-gateway:v0.9.x
docker-compose -f docker-compose.unified.yml up -d gateway
```

**包版本回滚**：

```bash
# 回滚 Python 包
pip install engram==0.9.x

# 或在 requirements.txt 中锁定版本
echo "engram==0.9.x" >> requirements.txt
pip install -r requirements.txt
```

### 6.3 数据兼容性

| 数据 | 向前兼容 | 向后兼容 |
|------|----------|----------|
| `governance.write_audit` | ✓ v0.9 可读 v1.0 记录 | ✓ v1.0 可读 v0.9 记录 |
| `outbox_memory` | ✓ | ✓ |
| `evidence_refs_json` | ✓ | ✓ |

**说明**：本次升级仅涉及代码层 DI 重构，不涉及数据库 Schema 变更，数据完全兼容。

---

## 7. 测试迁移示例

### 7.1 v0.9 Legacy 方式（不推荐）

```python
# tests/gateway/test_memory_store_legacy.py
async def test_memory_store_with_legacy_params():
    """v0.9 兼容方式（会产生 DeprecationWarning）"""
    result = await memory_store_impl(
        payload_md="test content",
        correlation_id="corr-test000",
        _config=mock_config,      # legacy
        _db=mock_db,              # legacy
        _openmemory_client=mock_client,  # legacy
    )
    assert result.ok
```

### 7.2 v1.0 推荐方式

```python
# tests/gateway/test_memory_store.py
from engram.gateway.di import GatewayDeps

async def test_memory_store_with_deps():
    """v1.0 推荐方式"""
    deps = GatewayDeps.for_testing(
        config=mock_config,
        logbook_adapter=mock_adapter,
        openmemory_client=mock_client,
    )
    result = await memory_store_impl(
        payload_md="test content",
        correlation_id="corr-test000",
        deps=deps,  # 必需参数
    )
    assert result.ok
```

### 7.3 集成测试方式

```python
# tests/gateway/test_integration.py
from engram.gateway.container import GatewayContainer, set_container, reset_container

@pytest.fixture(autouse=True)
def setup_test_container():
    """集成测试使用真实容器"""
    container = GatewayContainer.create_for_testing(
        config=real_config,
        logbook_adapter=real_adapter,
    )
    set_container(container)
    yield
    reset_container()

async def test_memory_store_integration():
    """集成测试通过 HTTP 端点调用"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post("/memory/store", json={
            "payload_md": "test content",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
```

---

## 8. 常见问题

### Q1: 升级后测试报 `TypeError: missing required argument 'deps'`

**原因**：测试代码仍使用 v0.9 的可选参数调用方式。

**解决**：使用 `GatewayDeps.for_testing()` 创建 deps 并传入：

```python
deps = GatewayDeps.for_testing(config=mock_config, ...)
result = await memory_store_impl(..., deps=deps, correlation_id="corr-xxx")
```

### Q2: handlers 中能否直接调用 `get_gateway_deps()`？

**不推荐**。v1.0 要求 handlers 通过参数接收 `deps`，而不是内部获取。如果需要在 handler 内部获取依赖，应修改调用方（入口层）传入。

### Q3: 如何检测代码中是否还有 legacy 用法？

使用 grep 搜索：

```bash
# 搜索 legacy 参数
rg "_config=|_db=|_openmemory_client=" src/engram/gateway/

# 搜索 handlers 中的隐式依赖获取
rg "get_config\(\)|get_adapter\(\)|get_client\(\)|GatewayDeps.create\(\)" src/engram/gateway/handlers/
```

### Q4: v1.0 是否影响现有的 Cursor MCP 集成？

**不影响**。对外的 HTTP/MCP 契约保持不变，Cursor 调用 `/mcp` 端点的方式无需修改。

---

## 9. 相关文档

| 文档 | 说明 |
|------|------|
| [ADR: Gateway DI 与入口边界统一](../architecture/adr_gateway_di_and_entry_boundary.md) | 完整的 DI 架构设计 |
| [Gateway 设计](./06_gateway_design.md) | 整体架构与依赖注入迁移路径 |
| [Gateway 能力边界](./07_capability_boundary.md) | 对外契约定义 |
| [Gateway ↔ Logbook 边界](../contracts/gateway_logbook_boundary.md) | 接口签名契约 |

---

## 10. 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-01-31 | v1.0-draft | 初始版本 |
