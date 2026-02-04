# Memory Gateway（MCP）设计

> **术语说明**：Memory Gateway 是 Gateway 组件的完整名称，后续简称 Gateway。详见 [命名规范](../architecture/naming.md)。

## 目标
- Cursor 只连 Gateway（/mcp）
- Gateway 负责：
  1) team_write_enabled + policy 校验
  2) 写入裁剪（长度/证据链/去重）
  3) 写入审计（governance.write_audit）
  4) 失败降级（logbook.outbox_memory）
  5) promotion：个人/团队/公共提升队列（可选）

## 对外暴露的 MCP 工具（建议）
- memory_store(payload_md, target_space?, meta_json?) -> {action, space_written, memory_id?, evidence_refs}
- memory_query(query, spaces=[...], filters={owner,module,kind}, topk=...) -> results
- memory_promote(candidate_id, to_space, reason) -> promo_id
- memory_reinforce(memory_id, delta_md) -> ok

## 关键治理逻辑（写入）
- 默认 target_space = team:<project>
- 若 team_write_enabled=false：redirect -> private:<actor>
- 若策略不满足：redirect -> private:<actor> 或 reject（建议优先 redirect）

---

## Gateway 模块结构与入口职责

### 模块职责划分（单一事实来源）

| 模块 | 职责 | 状态 |
|------|------|------|
| **app.py** | HTTP 入口，FastAPI 应用定义 | 入口层（单一来源） |
| **startup.py** | 应用生命周期管理，依赖预热 | 启动层 |
| **container.py** | GatewayContainer 依赖容器，FastAPI 集成 | 依赖管理 |
| **di.py** | GatewayDeps/RequestContext，纯 Python DI | 依赖管理（推荐） |
| **mcp_rpc.py** | JSON-RPC 2.0 协议层，路由分发 | 协议层 |
| **handlers/** | 业务逻辑实现（memory_store, memory_query 等） | 业务层 |
| **logbook_db.py** | Logbook 数据库代理层（已弃用） | 代理层（兼容） |
| **logbook_adapter.py** | Logbook 适配器，对接 engram_logbook | 适配层 |

### correlation_id 单一来源原则

```
┌─────────────────────────────────────────────────────────────────────┐
│                        HTTP 入口层 (app.py)                          │
│   correlation_id = generate_correlation_id()  ← 唯一生成点            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     mcp_rpc.py (dispatch)                            │
│   set_current_correlation_id(correlation_id)  ← contextvars 传递     │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     handlers (业务逻辑)                               │
│   corr_id = get_current_correlation_id()  ← 获取，不生成              │
│   或 corr_id = params["correlation_id"]                              │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     响应 (success/error)                             │
│   response.correlation_id = corr_id  ← 保持一致                      │
└─────────────────────────────────────────────────────────────────────┘
```

**契约约束**：
1. correlation_id 只在 HTTP 入口层生成一次
2. handlers 必须使用传入的 correlation_id，不自行生成
3. 错误响应中的 correlation_id 必须与请求保持一致
4. 薄代理层（如 logbook_db.py）只转发调用，不引入新 correlation_id

### 依赖注入迁移路径

> **详细设计**：完整的 DI 架构、分层图和迁移计划参见 [ADR: Gateway DI 与入口边界统一](../architecture/adr_gateway_di_and_entry_boundary.md)。
>
> **v2.0 升级指南**：Legacy 参数移除、签名变更和迁移检查清单参见 [v2.0 升级指南：移除 Handler DI 兼容层](./upgrade_v2_0_remove_handler_di_compat.md)。

**推荐方式（生产环境）**：

```python
# 生产路径：使用 get_gateway_deps() 获取绑定到全局容器的 deps
from engram.gateway.container import get_gateway_deps

deps = get_gateway_deps()
config = deps.config
adapter = deps.logbook_adapter  # LogbookAdapter（推荐）
client = deps.openmemory_client
```

**已弃用方式**：

```python
# 禁止：直接导入模块级获取函数（handlers 内禁止）
from engram.gateway.logbook_db import get_db         # 已弃用
from engram.gateway.config import get_config         # 应通过 deps.config 获取
from engram.gateway.container import get_container   # 应由入口层调用，handlers 禁止使用
from engram.gateway.logbook_adapter import get_adapter  # 应通过 deps.logbook_adapter 获取

# 推荐：通过 get_gateway_deps() 或入口层显式传递 deps 参数
from engram.gateway.container import get_gateway_deps
deps = get_gateway_deps()
```

> **重要约束**：`handlers/` 模块内禁止直接 import `get_container()`。
> 容器管理应由入口层（`app.py`、`startup.py`）负责，handlers 通过 `deps` 参数接收依赖。

**测试场景**：

```python
# 单测路径：使用 GatewayDeps.for_testing() 注入 mock
from engram.gateway.di import GatewayDeps

deps = GatewayDeps.for_testing(
    config=mock_config,
    logbook_adapter=mock_adapter,
    openmemory_client=mock_client,
)
result = await memory_store_impl(..., deps=deps, correlation_id="corr-test000")
```

### 最佳实践

| 场景 | 推荐方式 | 说明 |
|------|----------|------|
| **生产环境** | `get_gateway_deps()` | 获取绑定到全局容器的 deps，共享依赖实例 |
| **单元测试** | `GatewayDeps.for_testing(...)` | 显式注入 mock 对象，完全隔离外部依赖 |
| **集成测试** | `GatewayContainer.create_for_testing(...)` | 使用真实依赖但可替换部分组件 |

**关键原则**：

1. **handlers 禁止直接导入隐式依赖**：不要在 handlers 中 `from ... import get_config/get_client/get_container`
2. **correlation_id 由入口层生成**：handlers 接收 `correlation_id` 参数，不自行调用 `generate_correlation_id()`
3. **依赖通过 deps 参数传递**：所有依赖通过 `deps: GatewayDeps` 参数获取，提高可测试性

---

## Gateway ↔ Logbook 边界与数据流

### 架构边界
- **Logbook (engram_logbook)**: 本地 PostgreSQL 数据库层，负责治理设置、审计日志、失败补偿队列（outbox）
- **Gateway**: MCP 网关层，负责策略校验、写入裁剪、与 OpenMemory 的交互
- **数据流向**: Cursor → Gateway → OpenMemory + Logbook(持久化/降级)

### Gateway 依赖 Logbook 的原语接口

Gateway 依赖 `engram_logbook` 提供的原语接口进行治理、审计和失败补偿。

→ **完整接口列表与签名**：[docs/contracts/gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md#logbook-原语接口由-engram_logbook-提供)

→ **降级契约约束**：[docs/contracts/gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md#降级契约)

接口分类：
- **治理模块** (`engram_logbook.governance`)：设置读写、审计记录
- **Outbox 模块** (`engram_logbook.outbox`)：入队、租约、确认、重试、死信
- **URI 模块** (`engram_logbook.uri`)：Evidence URI 构建与解析

### 数据流示意

```
┌─────────┐    memory_store     ┌──────────────┐
│ Cursor  │ ─────────────────>  │   Gateway    │
│  (MCP)  │ <─────────────────  │              │
└─────────┘    response         └──────┬───────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                  │
                    ▼                  ▼                  ▼
            ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
            │  OpenMemory  │   │   Logbook DB   │   │   Logbook DB   │
            │   (写入)     │   │ write_audit  │   │   outbox     │
            └──────────────┘   └──────────────┘   └──────────────┘
                  ↑                                      │
                  │       失败重试                        │
                  └──────────────────────────────────────┘
```

---

## Audit / Outbox / Reliability Report 一致性闭环

Gateway 通过 audit、outbox 和 reliability_report 三者协同，确保写入操作的可追溯性和最终一致性。

> **相关 ADR**：审计记录的原子性保证方案见 [ADR: Gateway 审计原子性](../architecture/adr_gateway_audit_atomicity.md)。

### 闭环关系

```
                    ┌───────────────────────────────────────────────┐
                    │              Gateway 写入操作                  │
                    └───────────────────┬───────────────────────────┘
                                        │
                    ┌───────────────────┴───────────────────┐
                    │                                       │
                    ▼                                       ▼
            ┌──────────────┐                        ┌──────────────┐
            │    Audit     │                        │    Outbox    │
            │  (写入审计)   │                        │  (失败缓冲)   │
            └──────┬───────┘                        └──────┬───────┘
                   │                                       │
                   │   统计聚合                              │
                   ▼                                       ▼
            ┌────────────────────────────────────────────────────┐
            │              Reliability Report                    │
            │   (汇总 audit 结果 + outbox 状态 → 可靠性度量)      │
            └────────────────────────────────────────────────────┘
```

### 一致性保证

| 组件 | 写入时机 | 一致性约束 |
|------|----------|------------|
| **Audit** | 每次写入操作（无论成功/失败/redirect） | 必须与操作同步写入，不可丢失 |
| **Outbox** | 仅在 OpenMemory 写入失败时 | 入队必须在 audit 记录之后 |
| **Reliability Report** | 定期聚合或按需生成 | 必须基于 audit + outbox 的完整数据 |

### 闭环校验规则

```
invariant: audit.count(action=redirect) == outbox.count(status in [pending, sent, dead])
invariant: reliability_report.total_writes == audit.count(*)
invariant: reliability_report.success_rate == audit.count(action=allow) / audit.count(*)
```

**不变量映射表**（审计 reason/action 与 outbox 状态）：

| outbox 状态 | 审计 reason | 审计 action | 说明 |
|-------------|-------------|-------------|------|
| sent | `outbox_flush_success` 或 `outbox_flush_dedup_hit` | allow | flush 成功 |
| dead | `outbox_flush_dead` | reject | 重试耗尽 |
| pending (stale) | `outbox_stale` | redirect | 锁过期，需重新调度 |

**SQL 查询契约**：`evidence_refs_json->>'outbox_id'` 必须能定位审计记录（outbox_id 需在顶层）。

**测试互证**：上述不变量由集成测试验证，参见：
- [`tests/gateway/test_reconcile_outbox.py::TestAuditOutboxInvariants`](../../tests/gateway/test_reconcile_outbox.py) - 完整闭环测试
- [`tests/gateway/test_audit_event_contract.py::TestEvidenceRefsJsonLogbookQueryContract`](../../tests/gateway/test_audit_event_contract.py) - evidence_refs_json 顶层字段契约测试

### 异常处理

- **Audit 写入失败**：Gateway 应阻止主操作继续，避免不可审计的写入
- **Outbox 入队失败**：记录到 audit 并返回错误，确保可追溯
- **Report 生成失败**：不影响主流程，但应触发告警
