# Gateway 契约收敛文档

> 版本: v1.0  
> 生效日期: 2026-02-01  
> 适用于: `src/engram/gateway/`

## 概述

本文档汇总 Gateway 的核心契约不变量、允许变更与禁止变更，按五个域组织：

1. **MCP JSON-RPC** - JSON-RPC 2.0 协议层契约
2. **AuditEvent** - 审计事件结构与语义契约
3. **Outbox & Reconcile** - 补偿队列与对账契约
4. **Error Codes** - 错误码与错误原因码契约
5. **Gateway 路由策略** - OpenMemory→Outbox 路由决策与错误语义契约

每条不变量都附有对应的测试锚点，确保契约可验证。

### Gateway-Logbook 桥接模块说明

> **重要变更 (v1.0+)**：`engram.gateway.logbook_db` 模块已废弃并删除。
>
> Gateway 与 Logbook 的桥接现在统一通过 `engram.gateway.logbook_adapter` 模块实现。
>
> | 状态 | 模块路径 | 说明 |
> |------|----------|------|
> | ❌ 已删除 | `engram.gateway.logbook_db` | v1.0 前的旧桥接模块，不再支持 |
> | ✅ 推荐 | `engram.gateway.logbook_adapter` | 当前唯一的 Gateway-Logbook 桥接模块 |
>
> 相关文档：
> - [deprecated_logbook_db_references_ssot.md](../architecture/deprecated_logbook_db_references_ssot.md)
> - [gateway_logbook_boundary.md](./gateway_logbook_boundary.md)

---

## 1. MCP JSON-RPC 域

### 1.1 不变量

| 编号 | 不变量 | 说明 | 测试锚点 |
|------|--------|------|----------|
| MCP-INV-01 | JSON-RPC 2.0 协议格式必须符合规范 | 请求必须包含 `jsonrpc: "2.0"`、`method`、`id` 字段 | `tests/gateway/test_mcp_jsonrpc_contract.py::TestJsonRpcInvalidRequest` |
| MCP-INV-02 | `tools/list` 必须返回完整的工具清单 | 返回的 tools 数组必须包含所有已注册工具 | `tests/gateway/test_mcp_jsonrpc_contract.py::TestToolsList` |
| MCP-INV-03 | `tools/call` 参数校验必须返回 -32602 | 缺少 `name` 或 `name` 不存在时返回 INVALID_PARAMS | `tests/gateway/test_mcp_jsonrpc_contract.py::TestToolsCallErrorAlignment` |
| MCP-INV-04 | 错误响应必须包含结构化 `error.data` | ErrorData 必须包含 category/reason/retryable/correlation_id | `tests/gateway/test_mcp_jsonrpc_contract.py::TestErrorDataStructure` |
| MCP-INV-05 | correlation_id 格式必须为 `corr-{16位十六进制}` | 总长度 21 字符，用于全链路追踪 | `tests/gateway/test_mcp_jsonrpc_contract.py::TestCorrelationIdHeaderAlignment` |
| MCP-INV-06 | 同一请求的 correlation_id 必须唯一且一致 | 入口生成后传递到所有子调用，确保审计可追溯 | `tests/gateway/test_mcp_jsonrpc_contract.py::TestCorrelationIdSingleSourceContract` |
| MCP-INV-07 | 旧协议格式必须被兼容处理 | `{tool, arguments}` 格式自动识别并正确路由 | `tests/gateway/test_mcp_jsonrpc_contract.py::TestLegacyProtocolComplete` |
| MCP-INV-08 | 错误码与分类必须一一对应 | -32700→protocol, -32602→validation, -32001→dependency 等 | `tests/gateway/test_mcp_jsonrpc_contract.py::TestErrorDataContractCompliance` |
| MCP-INV-09 | ErrorReason 公开常量必须与白名单一致 | 通过反射提取公开常量，与 VALID_ERROR_REASONS 完全相等 | `tests/gateway/test_mcp_jsonrpc_contract.py::TestErrorReasonWhitelistConsistency` |

### 1.2 允许变更

| 变更类型 | 说明 | 约束 |
|----------|------|------|
| 新增工具 | 可在 `AVAILABLE_TOOLS` 中注册新工具 | 必须提供 `name`、`description`、`inputSchema` |
| 新增 ErrorReason | 可新增错误原因码 | 必须遵循命名规范（见 §4.3） |
| 扩展 ErrorData.details | 可在 details 中添加附加诊断信息 | 不得影响现有字段的语义 |
| 新增可选响应字段 | 工具返回结果可新增可选字段 | 必须向后兼容，旧客户端可忽略 |

### 1.3 禁止变更

| 禁止变更 | 原因 | 测试保护 |
|----------|------|----------|
| ❌ 删除或重命名现有工具 | 破坏客户端兼容性 | `TestToolsList` |
| ❌ 修改 JSON-RPC 错误码映射 | 客户端依赖固定错误码进行重试决策 | `TestErrorDataContractCompliance` |
| ❌ 修改 correlation_id 格式 | 日志聚合和监控系统依赖固定格式 | `TestCorrelationIdHeaderAlignment` |
| ❌ 移除 ErrorData 必需字段 | 调用方依赖 category/reason/retryable 进行错误处理 | `TestErrorDataFields` |
| ❌ 修改工具的必需参数 | 现有客户端调用会失败 | `TestToolsCall` |
| ❌ 移除旧协议兼容支持 | 存量 MCP 客户端依赖旧格式 | `TestLegacyVsJsonRpcCoexistence` |

---

## 2. AuditEvent 域

### 2.1 不变量

| 编号 | 不变量 | 说明 | 测试锚点 |
|------|--------|------|----------|
| AUDIT-INV-01 | 审计事件必须遵循 schema v1.x 结构 | schema_version 主版本号变更需迁移脚本 | `tests/gateway/test_audit_event_contract.py::TestSchemaVersionGuardrail` |
| AUDIT-INV-02 | source 字段必须为枚举值 | gateway / outbox_worker / reconcile_outbox | `tests/gateway/test_audit_event_contract.py::TestSourceEnum` |
| AUDIT-INV-03 | event_ts 必须为 ISO 8601 格式 | 毫秒精度，UTC 时区 | `tests/gateway/test_audit_event_contract.py::TestEventTsFormat` |
| AUDIT-INV-04 | decision 子结构必须包含 action 和 reason | 决策信息用于统计和追溯 | `tests/gateway/test_audit_event_contract.py::TestDecisionSubstructure` |
| AUDIT-INV-05 | evidence_summary 必须包含 count/has_strong/uris | 证据摘要用于快速判定证据强度 | `tests/gateway/test_audit_event_contract.py::TestEvidenceSummarySubstructure` |
| AUDIT-INV-06 | evidence_refs_json 顶层必须包含 SQL 查询字段 | outbox_id/source/correlation_id/payload_sha 支持 SQL 直接查询 | `tests/gateway/test_audit_event_contract.py::TestEvidenceRefsJsonLogbookQueryContract` |
| AUDIT-INV-07 | "审计优先"语义：先写审计再写 OpenMemory | 确保审计不可丢，即使 OpenMemory 写入失败也有记录 | `tests/gateway/test_audit_event_contract.py::TestAuditFirstSemantics` |
| AUDIT-INV-08 | OpenMemory 失败入 outbox 时审计 action=redirect | 审计内统一使用 redirect，通过 intended_action 区分 | `tests/gateway/test_audit_event_contract.py::TestOpenMemoryFailureAuditEventSchema` |
| AUDIT-INV-09 | policy/validation 子结构为稳定 v1.1 契约 | 包含 mode/mode_reason/policy_version/is_pointerized 等字段 | `tests/gateway/test_audit_event_contract.py::TestPolicySubstructure` |
| AUDIT-INV-10 | correlation_id 必须在 gateway_event 和顶层同时存在 | 支持嵌套查询和顶层快速查询两种模式 | `tests/gateway/test_audit_event_contract.py::TestEvidenceRefsJsonCorrelationIdConsistencyContract` |

### 2.1.1 evidence_refs_json 顶层必需字段清单 (AUDIT-INV-06)

> **禁止移除**：以下字段被 SQL 查询依赖，仅允许新增可选字段。
> **测试锚点**：`tests/gateway/test_audit_event_contract.py::TestEvidenceRefsJsonLogbookQueryContract`

#### gateway source

| 字段 | 说明 | SQL 查询示例 |
|------|------|--------------|
| `gateway_event` | 必须包含完整审计元数据 | `evidence_refs_json->'gateway_event'` |
| `source` | 事件来源 | `evidence_refs_json->>'source'` |
| `correlation_id` | 关联追踪 ID | `evidence_refs_json->>'correlation_id'` |

#### outbox_worker source

| 字段 | 说明 | SQL 查询示例 |
|------|------|--------------|
| `gateway_event` | 必须包含完整审计元数据 | `evidence_refs_json->'gateway_event'` |
| `source` | 事件来源 | `evidence_refs_json->>'source'` |
| `correlation_id` | 关联追踪 ID | `evidence_refs_json->>'correlation_id'` |
| `outbox_id` | Outbox 记录 ID | `(evidence_refs_json->>'outbox_id')::int` |
| `extra` | 包含 worker_id, attempt_id, correlation_id | `evidence_refs_json->'extra'->>'worker_id'` |

#### reconcile_outbox source

| 字段 | 说明 | SQL 查询示例 |
|------|------|--------------|
| `gateway_event` | 必须包含完整审计元数据 | `evidence_refs_json->'gateway_event'` |
| `source` | 事件来源 | `evidence_refs_json->>'source'` |
| `correlation_id` | 关联追踪 ID | `evidence_refs_json->>'correlation_id'` |
| `outbox_id` | Outbox 记录 ID | `(evidence_refs_json->>'outbox_id')::int` |
| `extra` | 包含 reconciled, original_locked_by 等 | `evidence_refs_json->'extra'->>'reconciled'` |

### 2.1.2 evidence_refs_json 语义锚点列表

> **定义**：语义锚点是 evidence_refs_json 顶层的标准化字段，用于跨阶段关联查询和全链路追踪。
> **测试锚点**：`tests/gateway/test_audit_event_contract.py::TestEvidenceRefsJsonSemanticAnchors`

#### 语义锚点完整列表

| 锚点字段 | 数据类型 | 必需性 | 语义说明 | 写入时机 |
|----------|----------|--------|----------|----------|
| `correlation_id` | `string` | 必需 | 请求全链路追踪 ID，格式 `corr-{16位十六进制}` | 请求入口生成 |
| `outbox_id` | `integer` | 条件必需 | Outbox 记录 ID，redirect/outbox_worker 场景必需 | OpenMemory 失败入队时 |
| `memory_id` | `string` | 条件必需 | OpenMemory 记录 ID，success 场景必需 | OpenMemory 写入成功时 |
| `payload_sha` | `string` | 必需 | 内容 SHA256 哈希，用于去重和关联 | 审计写入时 |
| `source` | `string` | 必需 | 事件来源，枚举：gateway/outbox_worker/reconcile_outbox | 审计写入时 |
| `extra` | `object` | 可选 | 扩展信息容器，包含场景特定字段 | 按需写入 |

#### 各锚点详细规范

##### correlation_id

全链路追踪的核心锚点，确保从请求入口到最终审计可追溯。

| 属性 | 规范 |
|------|------|
| 格式 | `corr-{16位十六进制}`，总长度 21 字符 |
| 生成位置 | `middleware.py:CorrelationIdMiddleware` |
| 唯一性 | 同一请求链路内唯一 |
| 传递路径 | HTTP Header → contextvars → audit → outbox → reconcile |

**SQL 查询示例**：

```sql
-- 按 correlation_id 查询完整审计链路
SELECT audit_id, action, reason, status, created_at
FROM governance.write_audit
WHERE evidence_refs_json->>'correlation_id' = 'corr-a1b2c3d4e5f67890'
ORDER BY created_at;

-- 关联 outbox 记录
SELECT w.audit_id, w.action, o.status as outbox_status
FROM governance.write_audit w
JOIN logbook.outbox_memory o 
  ON (w.evidence_refs_json->>'outbox_id')::int = o.outbox_id
WHERE w.evidence_refs_json->>'correlation_id' = 'corr-a1b2c3d4e5f67890';
```

**测试锚点**：`test_mcp_jsonrpc_contract.py::TestCorrelationIdSingleSourceContract`

##### outbox_id

Outbox 记录关联锚点，用于 redirect/降级场景的审计追溯。

| 属性 | 规范 |
|------|------|
| 类型 | 整数（Outbox 表主键） |
| 必需场景 | source=outbox_worker 或 source=reconcile_outbox |
| 写入时机 | finalize_audit 时通过 evidence_refs_json_patch 写入 |

**SQL 查询示例**：

```sql
-- 按 outbox_id 查询关联审计
SELECT * FROM governance.write_audit
WHERE (evidence_refs_json->>'outbox_id')::int = 123;

-- 检查 outbox 是否有对应审计
SELECT o.outbox_id, o.status, w.audit_id
FROM logbook.outbox_memory o
LEFT JOIN governance.write_audit w 
  ON (w.evidence_refs_json->>'outbox_id')::int = o.outbox_id
  AND w.reason LIKE 'outbox_flush_%'
WHERE o.status IN ('sent', 'dead');
```

**测试锚点**：`test_reconcile_outbox.py::TestAuditOutboxInvariants`

##### memory_id

OpenMemory 记录关联锚点，用于 success 场景的数据追溯。

| 属性 | 规范 |
|------|------|
| 类型 | 字符串（OpenMemory 返回的 ID） |
| 必需场景 | status=success 且 action=allow |
| 写入时机 | finalize_audit 时通过 evidence_refs_json_patch 写入 |

**SQL 查询示例**：

```sql
-- 按 memory_id 查询审计记录
SELECT * FROM governance.write_audit
WHERE evidence_refs_json->>'memory_id' = 'mem-abc123';

-- 统计成功写入的 memory_id 分布
SELECT DATE(created_at), COUNT(DISTINCT evidence_refs_json->>'memory_id')
FROM governance.write_audit
WHERE status = 'success' AND evidence_refs_json->>'memory_id' IS NOT NULL
GROUP BY DATE(created_at);
```

**测试锚点**：`test_audit_event_contract.py::TestFinalizeWritebackMemoryId`

##### payload_sha

内容去重锚点，基于 SHA256 哈希确保同一内容不重复写入。

| 属性 | 规范 |
|------|------|
| 类型 | 字符串（64 字符十六进制 SHA256） |
| 计算方式 | `sha256(payload_md.encode('utf-8')).hexdigest()` |
| 用途 | 去重检查、内容关联 |

**SQL 查询示例**：

```sql
-- 按 payload_sha 查询所有相关审计（含重试）
SELECT audit_id, action, reason, status, created_at
FROM governance.write_audit
WHERE evidence_refs_json->>'payload_sha' = 'a1b2c3...'
ORDER BY created_at;

-- 检查去重命中
SELECT COUNT(*) as dedup_count
FROM governance.write_audit
WHERE reason = 'outbox_flush_dedup_hit'
  AND evidence_refs_json->>'payload_sha' = 'a1b2c3...';
```

**测试锚点**：`test_outbox_worker.py::TestDeduplication`

##### source

事件来源锚点，标识审计记录的产生位置。

| 属性 | 规范 |
|------|------|
| 类型 | 枚举字符串 |
| 允许值 | `gateway`、`outbox_worker`、`reconcile_outbox` |
| 用途 | 区分同一 correlation_id 下不同阶段的审计 |

**SQL 查询示例**：

```sql
-- 按 source 分组统计
SELECT evidence_refs_json->>'source' as source, COUNT(*)
FROM governance.write_audit
GROUP BY evidence_refs_json->>'source';

-- 查询 outbox_worker 产生的所有审计
SELECT * FROM governance.write_audit
WHERE evidence_refs_json->>'source' = 'outbox_worker';
```

**测试锚点**：`test_audit_event_contract.py::TestSourceEnum`

##### extra

扩展信息容器，用于存储场景特定的诊断/追溯字段。

| 属性 | 规范 |
|------|------|
| 类型 | JSON 对象 |
| 用途 | 存储非标准化的场景特定字段 |
| 常见字段 | `worker_id`、`attempt_id`、`reconciled`、`original_locked_by` |

**SQL 查询示例**：

```sql
-- 查询 reconcile 补写的审计
SELECT * FROM governance.write_audit
WHERE evidence_refs_json->'extra'->>'reconciled' = 'true';

-- 按 worker_id 分组统计 outbox 处理量
SELECT evidence_refs_json->'extra'->>'worker_id' as worker, COUNT(*)
FROM governance.write_audit
WHERE evidence_refs_json->>'source' = 'outbox_worker'
GROUP BY evidence_refs_json->'extra'->>'worker_id';

-- 查询 stale 锁的原持有者
SELECT audit_id, 
       evidence_refs_json->'extra'->>'original_locked_by' as locked_by,
       evidence_refs_json->'extra'->>'original_locked_at' as locked_at
FROM governance.write_audit
WHERE reason LIKE 'outbox_stale%';
```

**测试锚点**：`test_reconcile_outbox.py::TestReconcileStaleRecords`

#### 语义锚点不变量

| 编号 | 不变量 | 说明 | 测试锚点 |
|------|--------|------|----------|
| ANCHOR-INV-01 | correlation_id 必须在所有审计中存在 | 全链路追踪依赖 | `TestEvidenceRefsJsonCorrelationIdConsistencyContract` |
| ANCHOR-INV-02 | outbox_id 在 redirect 审计中必须存在 | Outbox 关联依赖 | `TestAuditOutboxInvariants` |
| ANCHOR-INV-03 | memory_id 在 success 审计中应存在 | 写入追溯依赖 | `TestFinalizeWritebackMemoryId` |
| ANCHOR-INV-04 | payload_sha 格式必须为 64 字符十六进制 | 去重逻辑依赖 | `TestDeduplication` |
| ANCHOR-INV-05 | source 必须为枚举值之一 | 统计分类依赖 | `TestSourceEnum` |

### 2.2 允许变更

| 变更类型 | 说明 | 约束 |
|----------|------|------|
| schema 次版本号升级 | v1.0 → v1.1 → v1.2 | 仅新增可选字段，向后兼容 |
| 新增 decision.reason 值 | 可新增业务/校验/依赖层 reason | 必须遵循命名规范（见 §4.3） |
| 新增可选审计字段 | evidence_refs_json 内可新增字段 | 不得修改现有字段语义 |
| 新增 source 枚举值 | 可新增事件来源 | 必须更新 schema 和文档 |

### 2.3 禁止变更

| 禁止变更 | 原因 | 测试保护 |
|----------|------|----------|
| ❌ 删除或重命名 schema 必需字段 | 下游消费者解析失败 | `TestAuditEventSchema` |
| ❌ 修改 evidence_refs_json 顶层字段名 | SQL 查询依赖固定字段名 | `TestEvidenceRefsJsonLogbookQueryContract` |
| ❌ 修改 redirect/deferred 语义边界 | 统计和对账逻辑依赖固定语义 | `TestOpenMemoryFailureAuditEventSchema` |
| ❌ 移除 correlation_id 顶层提升 | reconcile_outbox 依赖顶层字段进行 SQL 查询 | `TestEvidenceRefsJsonCorrelationIdConsistencyContract` |
| ❌ 修改 action 枚举值 | allow/redirect/reject 为固定契约 | `TestDecisionSubstructure` |

---

## 3. Outbox & Reconcile 域

### 3.1 不变量

| 编号 | 不变量 | 说明 | 测试锚点 |
|------|--------|------|----------|
| OUTBOX-INV-01 | outbox 状态机：pending → sent \| dead | 终态不可回退 | `tests/gateway/test_outbox_worker.py::TestProcessResults` |
| OUTBOX-INV-02 | sent 状态必须有对应成功审计 | reason=outbox_flush_success 或 outbox_flush_dedup_hit | `tests/gateway/test_reconcile_outbox.py::TestReconcileSentRecords` |
| OUTBOX-INV-03 | dead 状态必须有对应拒绝审计 | reason=outbox_flush_dead, action=reject | `tests/gateway/test_reconcile_outbox.py::TestReconcileDeadRecords` |
| OUTBOX-INV-04 | stale 记录必须有对应审计并可重调度 | reason=outbox_stale, action=redirect | `tests/gateway/test_reconcile_outbox.py::TestReconcileStaleRecords` |
| OUTBOX-INV-05 | Audit ↔ Outbox 计数闭环 | redirect 审计数 == outbox(pending+sent+dead) 数 | `tests/gateway/test_reconcile_outbox.py::TestAuditOutboxInvariants` |
| OUTBOX-INV-06 | Lease 协议必须被遵守 | claim/ack/fail_retry/mark_dead 调用顺序 | `tests/gateway/test_outbox_worker.py::TestLeaseProtocolCalls` |
| OUTBOX-INV-07 | 去重检查基于 payload_sha | 同一 payload 不重复写入 OpenMemory | `tests/gateway/test_outbox_worker.py::TestDeduplication` |
| OUTBOX-INV-08 | reconcile 退出码契约 | 0=成功, 1=部分失败, 2=执行错误 | `tests/gateway/test_reconcile_outbox.py::TestReconcileSmokeTest` |
| OUTBOX-INV-09 | pending 审计超时必须被清理 | 超过 2 小时的 pending 审计标记为 failed | `tests/gateway/test_reconcile_outbox.py::TestReconcilePendingAuditTimeouts` |
| OUTBOX-INV-10 | finalize_audit 幂等性 | 多次调用不产生副作用 | `tests/gateway/test_reconcile_outbox.py::TestIdempotentFinalizeAudit` |
| OUTBOX-INV-11 | 两阶段协议：pending 前置条件 | 调用 OpenMemory 前必须先写入 pending 审计 | `tests/gateway/test_audit_event_contract.py::TestTwoPhaseAuditProtocol` |
| OUTBOX-INV-12 | 两阶段协议：finalize 幂等约束 | finalize 使用 `WHERE status='pending'` 确保仅更新一次 | `tests/gateway/test_reconcile_outbox.py::TestIdempotentFinalizeAudit` |
| OUTBOX-INV-13 | 单阶段 reject：禁止产生 pending | reject 决策直接写入 status='success' 的最终审计 | `tests/gateway/test_audit_event_contract.py::TestSinglePhaseRejectAudit` |
| OUTBOX-INV-14 | failed 状态是终态，不可回退 | reconcile 不会修改 failed 状态的审计 | `tests/gateway/test_reconcile_outbox.py::TestFailedStatusInvariant` |
| OUTBOX-INV-15 | failed 审计必须有诊断信息 | 包含 error_type 或 reconcile_action | `tests/gateway/test_reconcile_outbox.py::TestFailedStatusInvariant` |

### 3.1.1 两阶段审计协议详解

> **适用场景**：allow/redirect 决策（需要后续 OpenMemory 操作）
> **实现位置**：`src/engram/gateway/services/audit_service.py`
> **ADR 参考**：[adr_gateway_audit_atomicity.md](../architecture/adr_gateway_audit_atomicity.md)

#### 写 pending 的前置条件

两阶段审计协议的第一阶段必须满足以下前置条件：

| 前置条件 | 说明 | 验证方式 |
|----------|------|----------|
| correlation_id 已生成 | 必须有唯一的关联 ID 用于后续 finalize | `assert correlation_id is not None` |
| 策略决策已完成 | policy 已返回 allow/redirect | `assert action in ('allow', 'redirect')` |
| evidence_refs_json 已构建 | 包含完整的 gateway_event | schema 校验 |
| 数据库连接可用 | Logbook DB 可达 | 连接健康检查 |

**调用时序**：

```
1. 生成 correlation_id
2. 执行 policy 决策
3. 构建 evidence_refs_json
4. 调用 write_pending_audit_or_raise()  ← 阻断语义
5. 执行 OpenMemory 操作
6. 调用 finalize_audit()
```

**代码示例**（`audit_service.py:write_pending_audit_or_raise`）：

```python
audit_id = db.insert_audit(
    actor_user_id=actor_user_id,
    target_space=target_space,
    action=action,
    reason=reason,
    payload_sha=payload_sha,
    evidence_refs_json=evidence_refs_json,
    correlation_id=correlation_id,  # 必需
    status="pending",               # 第一阶段状态
)
```

#### finalize 幂等约束

finalize 阶段使用 `WHERE status='pending'` 条件确保幂等性：

| 约束 | SQL 实现 | 说明 |
|------|----------|------|
| 只更新 pending 状态 | `WHERE status = 'pending'` | 防止重复更新 |
| 基于 correlation_id 查找 | `WHERE correlation_id = %s` | 唯一标识 |
| 返回更新行数 | `RETURNING audit_id` | 判断是否成功 |

**SQL 示例**：

```sql
UPDATE governance.write_audit
SET status = %s,
    reason = COALESCE(reason, '') || %s,
    evidence_refs_json = COALESCE(evidence_refs_json, '{}'::jsonb) || %s::jsonb,
    updated_at = now()
WHERE correlation_id = %s
  AND status = 'pending'  -- 幂等约束
RETURNING audit_id;
```

**幂等性保证**：
- 首次调用：`updated_count = 1`，返回 `True`
- 重复调用：`updated_count = 0`，返回 `False`（记录已被更新）
- 并发调用：只有一个成功更新，其他返回 `False`

#### patch 合并策略

`evidence_refs_json_patch` 使用 PostgreSQL JSONB `||` 操作符合并到顶层：

| 策略 | 说明 | SQL 实现 |
|------|------|----------|
| 顶层合并 | patch 字段合并到 evidence_refs_json 顶层 | `evidence_refs_json \|\| %s::jsonb` |
| 后值覆盖 | 同名字段以 patch 值为准 | JSONB `\|\|` 右侧优先 |
| 原值保留 | patch 不包含的字段保持不变 | JSONB `\|\|` 保留左侧不冲突字段 |
| NULL 安全 | 原值为 NULL 时使用空对象 | `COALESCE(evidence_refs_json, '{}'::jsonb)` |

**常见 patch 字段**：

| 场景 | patch 内容 | 说明 |
|------|-----------|------|
| OpenMemory 成功 | `{"memory_id": "mem-xxx"}` | 记录 memory_id |
| OpenMemory 失败入队 | `{"outbox_id": 123, "intended_action": "allow"}` | 记录 outbox_id |
| 超时清理 | `{"timeout_detected_at": "...", "reconcile_action": "mark_failed_timeout"}` | 记录诊断信息 |

### 3.1.2 单阶段 reject 审计协议

> **适用场景**：reject 决策（策略拒绝，无后续操作）
> **核心原则**：reject 不产生 pending 状态，直接写入最终审计

#### 禁止产生 pending 的原因

| 原因 | 说明 |
|------|------|
| 无后续操作 | reject 不需要调用 OpenMemory，无需两阶段 |
| 语义清晰 | pending 表示"操作进行中"，reject 是终态 |
| 避免悬挂 | 无 finalize 调用，不会产生悬挂 pending |
| 统计准确 | Reliability Report 不需要排除 reject 的 pending |

#### 审计写入失败应阻断请求

reject 场景下审计写入失败时：

| 行为 | 说明 | 测试锚点 |
|------|------|----------|
| 抛出 AuditWriteError | 阻断请求返回 | `test_audit_event_contract.py::TestAuditFirstSemantics` |
| 返回 500 错误 | 客户端收到服务端错误 | `test_error_codes.py::TestAuditWriteFailure` |
| 记录详细日志 | 包含 correlation_id 等诊断信息 | 日志断言 |

**代码示例**（`audit_service.py:write_audit_or_raise`）：

```python
try:
    audit_id = db.insert_audit(
        actor_user_id=actor_user_id,
        target_space=target_space,
        action="reject",
        reason=reason,
        # 注意：不传 status 参数，默认为 'success'（最终状态）
    )
except Exception as e:
    # 审计写入失败，阻断请求
    raise AuditWriteError(
        message="审计写入失败，操作已阻断",
        original_error=e,
    )
```

**与 allow/redirect 的对比**：

| 决策 | 审计协议 | status 值 | 是否需要 finalize |
|------|----------|-----------|-------------------|
| allow | 两阶段 | pending → success/redirected | 是 |
| redirect | 两阶段 | pending → redirected | 是 |
| reject | 单阶段 | success（直接） | 否 |

### 3.1.3 failed 状态来源与处理策略

> **适用场景**：审计记录的终态标记，表示操作已失败且不可恢复
> **不变量参考**：OUTBOX-INV-14, OUTBOX-INV-15
> **测试锚点**：`tests/gateway/test_reconcile_outbox.py::TestFailedStatusInvariant`

#### failed 状态的来源

| 来源场景 | 触发条件 | reason_suffix | evidence_refs_json 诊断字段 |
|----------|----------|---------------|----------------------------|
| **OpenMemory 4xx 错误** | HTTP 400-499 客户端错误 | `:client_error:<status_code>` | `error_type`, `status_code`, `error_message` |
| **reconcile 超时清理** | pending 审计超过 `pending_audit_timeout_hours` | `:audit_timeout` | `reconcile_action`, `timeout_detected_at`, `stale_duration_seconds` |

#### reconcile 对 failed 状态的处理策略

| 策略 | 说明 |
|------|------|
| **不自动修复** | failed 是终态，reconcile 不会尝试将其改为其他状态 |
| **不二次处理** | reconcile 查询只针对 `status='pending'`，failed 记录被排除 |
| **诊断字段保留** | reconcile 超时清理时会追加诊断信息，但不覆盖已有字段 |

#### failed 与其他终态的区别

| 状态 | 语义 | 是否可重试 | 统计分类 |
|------|------|-----------|----------|
| `success` | 操作成功完成 | 否 | 计入成功率分子 |
| `redirected` | 操作已降级入队 outbox | 由 outbox_worker 重试 | 排除在成功率计算外 |
| `failed` | 操作失败且不可恢复 | 否 | 计入失败统计 |

#### 代码引用

| 场景 | 代码位置 |
|------|----------|
| 4xx 错误 finalize | `src/engram/gateway/handlers/memory_store.py::_handle_client_error` |
| 超时清理 | `src/engram/gateway/reconcile_outbox.py::mark_pending_audit_as_failed` |

### 3.2 审计/Outbox 状态映射表（SSOT）

> **单一事实来源**：本表格为 outbox→audit 映射的权威定义。
> **错误码来源**：`src/engram/logbook/errors.py:ErrorCode`
> **测试锚点**：`tests/gateway/test_reconcile_outbox.py::TestAuditOutboxInvariants`

| outbox 状态 | 审计 action | 审计 reason | ErrorCode 常量 | 测试锚点 |
|-------------|-------------|-------------|----------------|----------|
| `sent` | `allow` | `outbox_flush_success` | `OUTBOX_FLUSH_SUCCESS` | `TestAuditOutboxInvariants::test_outbox_flush_success_audit_invariant` |
| `sent` (dedup) | `allow` | `outbox_flush_dedup_hit` | `OUTBOX_FLUSH_DEDUP_HIT` | `TestAuditOutboxInvariants::test_outbox_dedup_audit_invariant` |
| `dead` | `reject` | `outbox_flush_dead` | `OUTBOX_FLUSH_DEAD` | `TestAuditOutboxInvariants::test_outbox_flush_dead_audit_invariant` |
| `dead` (api_4xx) | `reject` | `outbox_flush_dead` | `OUTBOX_FLUSH_DEAD` | `TestOpenMemoryAPIErrorHandling::test_api_4xx_first_attempt_marks_dead` |
| `pending` (stale) | `redirect` | `outbox_stale` | `OUTBOX_STALE` | `TestAuditOutboxInvariants::test_outbox_stale_audit_invariant` |
| `pending` (retry) | `redirect` | `outbox_flush_retry` | `OUTBOX_FLUSH_RETRY` | `TestAuditOutboxInvariants::test_outbox_flush_retry_audit_invariant` |
| `pending` (api_5xx) | `redirect` | `outbox_flush_retry` | `OUTBOX_FLUSH_RETRY` | `TestOpenMemoryAPIErrorHandling::test_api_5xx_triggers_retry` |
| `pending` (conflict) | `redirect` | `outbox_flush_conflict` | `OUTBOX_FLUSH_CONFLICT` | `TestAuditOutboxInvariants::test_outbox_flush_conflict_audit_invariant` |
| `pending` (db_timeout) | `redirect` | `outbox_flush_db_timeout` | `OUTBOX_FLUSH_DB_TIMEOUT` | `TestAuditOutboxInvariants::test_outbox_flush_db_timeout_audit_invariant` |
| `pending` (db_error) | `redirect` | `outbox_flush_db_error` | `OUTBOX_FLUSH_DB_ERROR` | `TestAuditOutboxInvariants::test_outbox_flush_db_error_audit_invariant` |

> **注意 (v1.0+)**：`api_4xx` 错误（HTTP 400-499）被视为不可恢复的客户端错误，首次尝试即标记为 `dead`，不进入重试队列。
> `api_5xx` 错误（HTTP 500+）被视为可恢复的服务端错误，进入正常重试路径。

### 3.3 允许变更

| 变更类型 | 说明 | 约束 |
|----------|------|------|
| 调整 stale 阈值 | 默认 600s 可配置 | 不得小于 60s |
| 调整 scan_window | 默认 24h 可配置 | 不得小于 1h |
| 新增 outbox reason | 可新增错误场景原因码 | 必须遵循 `outbox_flush_*` 或 `outbox_*` 前缀 |
| 调整重试策略 | 可修改退避算法和最大重试次数 | 必须更新文档 |

### 3.4 禁止变更

| 禁止变更 | 原因 | 测试保护 |
|----------|------|----------|
| ❌ 修改 outbox 状态机终态 | sent/dead 为终态，不可回退 | `TestProcessResults` |
| ❌ 修改 audit/outbox 状态映射 | 对账逻辑依赖固定映射 | `TestAuditOutboxInvariants` |
| ❌ 移除 reconcile 补写审计能力 | 数据一致性依赖对账 | `TestReconcileSentRecords`, `TestReconcileDeadRecords` |
| ❌ 修改 dedup 判定逻辑 | payload_sha 是去重唯一依据 | `TestDeduplication` |
| ❌ 跳过 pending 审计超时清理 | 悬挂审计影响统计准确性 | `TestReconcilePendingAuditTimeouts` |

---

## 4. Error Codes 域

### 4.1 不变量

| 编号 | 不变量 | 说明 | 测试锚点 |
|------|--------|------|----------|
| ERR-INV-01 | JSON-RPC 错误码必须使用标准或自定义范围 | -32700~-32600 标准，-32099~-32000 自定义 | `tests/gateway/test_error_codes.py::TestErrorCodeConstants` |
| ERR-INV-02 | ErrorCode 与 category 必须一一对应 | -32602→validation, -32001→dependency 等 | `tests/gateway/test_error_codes.py::TestErrorCodeConsistency` |
| ERR-INV-03 | ErrorReason 必须在对应 category 下唯一 | 同一 category 不得有重复 reason | `tests/gateway/test_error_codes.py::TestGatewayErrorCodes` |
| ERR-INV-04 | deferred 响应必须包含 outbox_id | action=deferred 时 outbox_id 为必需字段 | `tests/gateway/test_error_codes.py::TestDeferredOutboxScenarios` |
| ERR-INV-05 | correlation_id 必须出现在所有错误响应中 | 支持全链路追踪 | `tests/gateway/test_error_codes.py::TestCorrelationIdInErrorResponses` |
| ERR-INV-06 | retryable 字段必须准确反映重试建议 | dependency 错误通常 retryable=true | `tests/gateway/test_error_codes.py::TestErrorDataCorrelationIdContract` |
| ERR-INV-07 | Outbox 审计 reason 必须与 ErrorCode 枚举一致 | engram_logbook.errors:ErrorCode 为单一事实来源 | `tests/gateway/test_reconcile_outbox.py::TestReconcileReasonErrorCodeContract` |

### 4.2 ErrorCode 与 ErrorReason 映射表

#### 4.2.1 协议层 (protocol)

| ErrorCode | ErrorReason | 说明 | retryable |
|-----------|-------------|------|-----------|
| -32700 | `PARSE_ERROR` | JSON 解析失败 | false |
| -32600 | `INVALID_REQUEST` | JSON-RPC 请求格式无效 | false |
| -32601 | `METHOD_NOT_FOUND` | 请求的 method 不存在 | false |

#### 4.2.2 校验层 (validation)

| ErrorCode | ErrorReason | 说明 | retryable |
|-----------|-------------|------|-----------|
| -32602 | `MISSING_REQUIRED_PARAM` | 缺少必需参数 | false |
| -32602 | `INVALID_PARAM_TYPE` | 参数类型错误 | false |
| -32602 | `INVALID_PARAM_VALUE` | 参数值无效 | false |
| -32602 | `UNKNOWN_TOOL` | tools/call 中指定的工具不存在 | false |

#### 4.2.3 业务层 (business)

| ErrorCode | ErrorReason | 说明 | retryable |
|-----------|-------------|------|-----------|
| -32002 | `POLICY_REJECT` | 策略拒绝 | 取决于策略 |
| -32002 | `AUTH_FAILED` | 鉴权失败 | false |
| -32002 | `ACTOR_UNKNOWN` | 用户身份未知 | false |
| -32002 | `GOVERNANCE_UPDATE_DENIED` | 治理更新被拒绝 | false |

#### 4.2.4 依赖层 (dependency)

| ErrorCode | ErrorReason | 说明 | retryable |
|-----------|-------------|------|-----------|
| -32001 | `OPENMEMORY_UNAVAILABLE` | OpenMemory 服务不可用 | true |
| -32001 | `OPENMEMORY_CONNECTION_FAILED` | OpenMemory 连接失败 | true |
| -32001 | `OPENMEMORY_API_ERROR` | OpenMemory API 返回错误 | 5xx 可重试 |
| -32001 | `LOGBOOK_DB_UNAVAILABLE` | Logbook 数据库不可用 | true |
| -32001 | `LOGBOOK_DB_CHECK_FAILED` | Logbook 数据库检查失败 | false |

#### 4.2.5 内部层 (internal)

| ErrorCode | ErrorReason | 说明 | retryable |
|-----------|-------------|------|-----------|
| -32603 | `INTERNAL_ERROR` | 通用内部错误 | false |
| -32603 | `TOOL_EXECUTOR_NOT_REGISTERED` | 工具执行器未注册 | false |
| -32603 | `UNHANDLED_EXCEPTION` | 未处理的异常 | false |

### 4.3 ErrorReason 命名规范

| 层级 | 命名规则 | 示例 | 单一事实来源 |
|------|----------|------|--------------|
| **协议层** | 大写 + 下划线 | `PARSE_ERROR`, `METHOD_NOT_FOUND` | `src/engram/gateway/error_codes.py:McpErrorReason`（`mcp_rpc.py:ErrorReason` 为兼容别名） |
| **校验层** | 大写 + 下划线 | `MISSING_REQUIRED_PARAM`, `UNKNOWN_TOOL` | `src/engram/gateway/error_codes.py:McpErrorReason`（`mcp_rpc.py:ErrorReason` 为兼容别名） |
| **业务层** | 小写 + 下划线 | `policy_passed`, `team_write_disabled` | `policy.py` |
| **依赖层** | 大写 + 下划线 | `OPENMEMORY_CONNECTION_FAILED` | `src/engram/gateway/error_codes.py:McpErrorReason`（`mcp_rpc.py:ErrorReason` 为兼容别名） |
| **Outbox 层** | 小写 + 下划线 | `outbox_flush_success`, `outbox_stale` | `engram_logbook.errors:ErrorCode` |

### 4.4 ErrorCode 与 ErrorReason 使用场景

#### 4.4.1 正确示例

**场景 1：参数校验错误**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32602,
    "message": "缺少必需参数: name",
    "data": {
      "category": "validation",
      "reason": "MISSING_REQUIRED_PARAM",
      "retryable": false,
      "correlation_id": "corr-a1b2c3d4e5f67890",
      "details": {"param": "name"}
    }
  }
}
```

**场景 2：依赖服务不可用**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32001,
    "message": "OpenMemory 连接失败",
    "data": {
      "category": "dependency",
      "reason": "OPENMEMORY_CONNECTION_FAILED",
      "retryable": true,
      "correlation_id": "corr-b2c3d4e5f6789012",
      "details": {"service": "openmemory", "timeout_ms": 5000}
    }
  }
}
```

**场景 3：业务层拒绝（审计内）**

```json
{
  "decision": {
    "action": "reject",
    "reason": "team_write_disabled"
  }
}
```

**场景 4：Outbox 补偿成功（审计内）**

```json
{
  "decision": {
    "action": "allow",
    "reason": "outbox_flush_success"
  }
}
```

#### 4.4.2 禁止示例

| 禁止用法 | 原因 | 正确做法 |
|----------|------|----------|
| ❌ `reason: "error"` | 过于模糊，无法定位问题 | 使用具体原因码如 `INTERNAL_ERROR` |
| ❌ `reason: "OpenMemory failed"` | 非规范命名，含空格 | 使用 `OPENMEMORY_CONNECTION_FAILED` |
| ❌ `reason: "outbox-success"` | 连字符不符合命名规范 | 使用 `outbox_flush_success` |
| ❌ `code: -32000` (TOOL_EXECUTION_ERROR) | 已废弃，不再使用 | 使用 -32602 或 -32603 |
| ❌ `retryable: "yes"` | 类型错误，必须为布尔值 | 使用 `retryable: true` |
| ❌ 校验层使用小写 reason | 命名规范要求大写 | `UNKNOWN_TOOL` 而非 `unknown_tool` |
| ❌ Outbox 层使用大写 reason | 命名规范要求小写 | `outbox_flush_success` 而非 `OUTBOX_FLUSH_SUCCESS` |

### 4.5 允许变更

| 变更类型 | 说明 | 约束 |
|----------|------|------|
| 新增 ErrorReason | 可新增错误原因码 | 必须遵循命名规范，更新文档 |
| 新增 details 字段 | 可在 error.data.details 中添加诊断信息 | 不得影响现有字段 |
| 调整 retryable 建议 | 可根据实际情况调整 | 必须更新文档说明 |

### 4.6 禁止变更

| 禁止变更 | 原因 | 测试保护 |
|----------|------|----------|
| ❌ 修改 ErrorCode 数值 | 客户端依赖固定错误码 | `TestErrorCodeConstants` |
| ❌ 修改 ErrorCode↔category 映射 | 破坏错误分类语义 | `TestErrorCodeConsistency` |
| ❌ 删除现有 ErrorReason | 现有日志和监控依赖 | `TestGatewayErrorCodes` |
| ❌ 使用已废弃的 -32000 | TOOL_EXECUTION_ERROR 已废弃 | `TestErrorDataContractCompliance` |
| ❌ 移除 correlation_id 必需性 | 全链路追踪依赖 | `TestCorrelationIdInErrorResponses` |
| ❌ 新增 category 枚举值 | 闭合枚举，客户端依赖穷举 | `TestErrorDataContractCompliance` |

### 4.6.1 -32000 (TOOL_EXECUTION_ERROR) 废弃决策

> **决策状态**: 已废弃（v1.0+）  
> **详细 ADR**: [mcp_jsonrpc_error_v1.md §13.5](./mcp_jsonrpc_error_v1.md#135--32000-错误码最终决策)

| 项目 | 内容 |
|------|------|
| **废弃原因** | 语义模糊，无法区分参数问题与内部错误 |
| **迁移目标** | 参数问题 → `-32602`，内部错误 → `-32603` |
| **兼容策略** | v1.x 保留常量定义但不生成响应，v2.0 移除 |
| **测试保护** | `TestErrorDataContractCompliance::test_tool_execution_error_deprecated` |

#### 代码/测试/Schema 要求

| 类型 | 要求 | 说明 |
|------|------|------|
| **代码** | 禁止新增 `-32000` 错误响应生成 | 新代码应使用 `-32602`（参数问题）或 `-32603`（内部错误） |
| **测试** | 禁止新增期望 `-32000` 的测试用例 | 现有测试仅用于验证废弃常量存在，不验证响应生成 |
| **Schema** | `mcp_jsonrpc_error_v1.schema.json` 枚举暂时保留 | v2.0 时移除，避免破坏向后兼容性校验 |

**迁移检查清单**：
- [ ] 新增错误处理代码不使用 `McpErrorCode.TOOL_EXECUTION_ERROR`
- [ ] 新增契约测试不期望 `error.code === -32000`
- [ ] 客户端兼容层保留对 `-32000` 的降级处理（fallback 到 `-32603` 语义）

---

## 4.7 审计策略分层：handlers vs outbox_worker

### 4.7.1 分层策略概述

Gateway 的审计写入策略在不同层级有不同的语义和容错要求：

| 层级 | 审计失败时行为 | 原因 | 测试锚点 |
|------|---------------|------|----------|
| **handlers 层** | 阻塞并返回错误 | 审计优先（AUDIT-INV-07），确保合规性 | `test_audit_event_contract.py::TestAuditFirstSemantics` |
| **outbox_worker 层** | 记录日志后继续 | 核心状态更新优先，避免数据不一致 | `test_outbox_worker.py::TestAuditExceptionResilience` |

### 4.7.2 handlers 层：审计优先语义

在 handlers 层（如 `memory_store.py`）：
- **写入顺序**：先写审计，再写 OpenMemory
- **失败行为**：审计写入失败则整个请求失败
- **设计原因**：确保每次写入都有审计记录，满足合规要求

```
handlers 层流程：
  写入审计 → (失败则返回错误)
      ↓
  写入 OpenMemory → (失败则入 outbox，审计已有)
```

### 4.7.3 outbox_worker 层：核心流程优先语义

在 outbox_worker 层：
- **写入顺序**：先执行核心状态更新（fail_retry/mark_dead），再写审计
- **失败行为**：审计写入失败只记录日志，返回正常 ProcessResult
- **设计原因**：
  1. 核心状态更新（outbox 状态机）已完成，数据一致性已保证
  2. 避免因审计 DB 临时故障导致状态机卡死
  3. reconcile_outbox 可后续补写审计记录

```
outbox_worker 层流程：
  fail_retry / mark_dead → (核心状态已更新)
      ↓
  写入审计 → (失败则记录日志，不中断流程)
      ↓
  返回 ProcessResult
```

### 4.7.4 不变量

| 编号 | 不变量 | 说明 | 测试锚点 |
|------|--------|------|----------|
| AUDIT-LAYER-01 | handlers 层审计失败必须阻塞请求 | 确保合规性，先审计后写入 | `test_audit_event_contract.py::TestAuditFirstSemantics` |
| AUDIT-LAYER-02 | outbox_worker 审计失败不得阻塞核心流程 | fail_retry/mark_dead 已执行，审计失败只记日志 | `test_outbox_worker.py::TestAuditExceptionResilience` |
| AUDIT-LAYER-03 | outbox_worker 审计异常不得向上传播 | 调用方收到 ProcessResult 而非异常 | `test_outbox_worker.py::test_audit_exception_does_not_propagate_to_caller` |

### 4.7.5 允许变更

| 变更类型 | 说明 | 约束 |
|----------|------|------|
| 增强日志记录 | 可增加审计失败时的诊断信息 | 不得影响核心流程返回 |
| 新增监控指标 | 可添加审计失败计数器 | 用于运维告警 |

### 4.7.6 禁止变更

| 禁止变更 | 原因 | 测试保护 |
|----------|------|----------|
| ❌ handlers 层跳过审计 | 破坏合规性 | `TestAuditFirstSemantics` |
| ❌ outbox_worker 审计失败时中断核心流程 | 导致状态机卡死 | `TestAuditExceptionResilience` |
| ❌ outbox_worker 审计异常向上抛出 | 破坏调用方契约 | `test_audit_exception_does_not_propagate_to_caller` |

---

## 4.8 intended_action 与 conflict_intended_operation 字段定义

> **单一事实来源**：本节定义两个易混淆字段的语义边界，确保审计追踪的精确性。
> **Schema 权威**：`schemas/audit_event_v1.schema.json`

### 4.8.1 intended_action（策略层面原意动作）

| 属性 | 定义 |
|------|------|
| **语义** | 策略决策的原意动作 |
| **值域** | `"allow"` \| `"redirect"` \| `"reject"` |
| **写入来源** | Gateway `memory_store.py::_handle_openmemory_failure` |
| **使用场景** | OpenMemory 失败入队 outbox 时，记录策略原意 |
| **位置** | `evidence_refs_json.intended_action` (顶层) |

**示例**：策略决定 `allow`，但 OpenMemory 失败导致响应为 `deferred`：
- `response.action = "deferred"`
- `evidence_refs_json.intended_action = "allow"`

### 4.8.2 conflict_intended_operation（Worker 冲突追踪）

| 属性 | 定义 |
|------|------|
| **语义** | Worker 原本计划执行的操作 |
| **值域** | `"success"` \| `"retry"` \| `"dead"` \| `"dedup_hit"` \| `"exception_retry"` |
| **写入来源** | `outbox_worker.py::_handle_conflict` |
| **使用场景** | 冲突检测时记录 Worker 预期操作，用于诊断 |
| **位置** | `evidence_refs_json.conflict_intended_operation` (顶层) |

**示例**：Worker 预期执行 `success`（ack_sent），但发生 lease 冲突：
- `reason = "outbox_flush_conflict"`
- `evidence_refs_json.conflict_intended_operation = "success"`

### 4.8.3 字段对比

| 字段 | 所属层 | 值域 | 用途 |
|------|--------|------|------|
| `intended_action` | 策略层 | allow/redirect/reject | 追踪策略原意 |
| `conflict_intended_operation` | Worker 层 | success/retry/dead/... | 诊断冲突原因 |

### 4.8.4 测试锚点

- `tests/gateway/test_audit_event_contract.py::test_intended_action_at_top_level_for_redirect_deferred`
- `tests/gateway/test_outbox_worker_integration.py::TestConflictHandling`

---

## 5. MCP 工具路由实现详解

> 版本: v1.0
> 本节详细记录 JSON-RPC 方法分发相关的函数/类，包括关键函数名、输入输出、可注入依赖点和测试覆盖点。

### 5.1 请求处理流程概览

```
HTTP 请求
    ↓
CorrelationIdMiddleware (middleware.py)
    ↓ 生成 correlation_id, 存入 contextvars
mcp_endpoint (routes.py)
    ↓ 判断请求格式
    ├─ JSON-RPC 2.0: mcp_router.dispatch()
    │       ↓
    │   JsonRpcRouter.dispatch (mcp_rpc.py)
    │       ↓ method 路由
    │       ├─ tools/list → handle_tools_list
    │       └─ tools/call → handle_tools_call
    │                ↓
    │            get_tool_executor() → execute_tool (tool_executor.py)
    │                ↓
    │            handler 实现 (handlers/*.py)
    │
    └─ 旧协议: 直接调用 _execute_tool
```

### 5.2 核心组件详解

#### 5.2.1 JsonRpcRouter (mcp_rpc.py)

JSON-RPC 方法路由器，负责方法注册和请求分发。

| 方法 | 签名 | 说明 |
|------|------|------|
| `__init__` | `() -> None` | 初始化 handlers 字典 |
| `method` | `(name: str) -> Callable` | 装饰器，注册方法处理器 |
| `register` | `(name: str, handler: MethodHandler) -> None` | 手动注册方法处理器 |
| `has_method` | `(name: str) -> bool` | 检查方法是否存在 |
| `list_methods` | `() -> List[str]` | 列出所有已注册方法 |
| `dispatch` | `(request: JsonRpcRequest, correlation_id: Optional[str]) -> JsonRpcResponse` | 分发请求到对应处理器 |

**可注入依赖点**：
- 通过 `register()` 或 `@router.method()` 装饰器注册自定义 handler
- handler 类型: `Callable[[Dict[str, Any]], Awaitable[Any]]`

**测试覆盖**：
- `tests/gateway/test_mcp_jsonrpc_contract.py::TestJsonRpcInvalidRequest`
- `tests/gateway/test_mcp_jsonrpc_contract.py::TestToolsCallErrorAlignment`

#### 5.2.2 handle_tools_call (mcp_rpc.py)

处理 `tools/call` JSON-RPC 请求。

| 项目 | 说明 |
|------|------|
| **位置** | `src/engram/gateway/mcp_rpc.py:1501-1564` |
| **输入** | `params: Dict[str, Any]` 包含 `{name: str, arguments: dict}` |
| **输出** | `{content: [{type: "text", text: "<json_result>"}]}` (MCP 格式) |
| **依赖获取** | `get_tool_executor()` 获取工具执行器 |
| **correlation_id** | 通过 `get_current_correlation_id()` 从 contextvars 获取 |

**参数校验 (ToolCallError)**：
- `MISSING_REQUIRED_PARAM`: 缺少 `name` 参数
- `INVALID_PARAM_TYPE`: `name` 不是字符串，或 `arguments` 不是对象
- `UNKNOWN_TOOL`: 工具名不在 `AVAILABLE_TOOLS` 中

**测试覆盖**：
- `tests/gateway/test_mcp_jsonrpc_contract.py::TestToolsCallErrorAlignment`
- `tests/gateway/test_mcp_jsonrpc_contract.py::TestErrorDataContractCompliance`

#### 5.2.3 handle_tools_list (mcp_rpc.py)

处理 `tools/list` JSON-RPC 请求。

| 项目 | 说明 |
|------|------|
| **位置** | `src/engram/gateway/mcp_rpc.py:1567-1586` |
| **输入** | `params: Dict[str, Any]` (未使用) |
| **输出** | `{tools: [ToolDefinition...]}` |
| **依赖** | `entrypoints.tool_executor.list_tools()` |

**测试覆盖**：
- `tests/gateway/test_mcp_jsonrpc_contract.py::TestToolsList`

#### 5.2.4 create_mcp_router / mcp_router (mcp_rpc.py)

| 项目 | 说明 |
|------|------|
| **create_mcp_router** | `() -> JsonRpcRouter` 创建并注册 tools/list、tools/call |
| **mcp_router** | 模块级默认路由器实例 |

### 5.3 路由注册层 (routes.py)

#### 5.3.1 register_routes

统一注册所有 Gateway 路由的入口函数。

| 项目 | 说明 |
|------|------|
| **位置** | `src/engram/gateway/routes.py:76-388` |
| **签名** | `register_routes(app: FastAPI) -> None` |

**主要职责**：
1. 注册 MinIO Audit Webhook router
2. 定义 `_execute_tool` 薄包装函数
3. 调用 `register_tool_executor(_execute_tool)` 注册工具执行器
4. 定义并注册各路由端点 (health, mcp, memory/*, reliability/report, governance/*)

**可注入依赖点**：
- 工具执行器通过 `register_tool_executor()` 全局注册
- `_execute_tool` 内部通过 `get_deps_for_request()` 延迟获取依赖

#### 5.3.2 mcp_endpoint

MCP 统一入口，双协议兼容。

| 项目 | 说明 |
|------|------|
| **位置** | `src/engram/gateway/routes.py:174-298` |
| **路由** | `POST /mcp` |
| **输入** | HTTP Request body (JSON) |
| **输出** | JSONResponse (JSON-RPC 或旧协议格式) |

**协议识别**：
- JSON-RPC 2.0: `body.get("jsonrpc") == "2.0" and "method" in body`
- 旧协议: `{tool: str, arguments: dict}` 格式

**处理流程**：
1. 通过 `get_request_correlation_id_or_new()` 获取 correlation_id
2. JSON-RPC 分支: 调用 `mcp_router.dispatch(rpc_request, correlation_id)`
3. 旧协议分支: 调用 `_execute_tool(tool, args, correlation_id)`

**测试覆盖**：
- `tests/gateway/test_mcp_jsonrpc_contract.py::TestLegacyProtocolComplete`
- `tests/gateway/test_mcp_jsonrpc_contract.py::TestCorrelationIdHeaderAlignment`

### 5.4 工具执行层 (entrypoints/tool_executor.py)

#### 5.4.1 execute_tool

MCP 工具执行核心入口。

| 项目 | 说明 |
|------|------|
| **位置** | `src/engram/gateway/entrypoints/tool_executor.py:57-192` |
| **签名** | `async execute_tool(tool: str, args: Dict, *, correlation_id: str, get_deps: Callable) -> Dict` |

**输入参数**：
- `tool`: 工具名称 (memory_store, memory_query, reliability_report, governance_update, evidence_upload)
- `args`: 工具参数字典
- `correlation_id`: 请求追踪 ID
- `get_deps`: 获取依赖的回调函数 (延迟调用)

**输出**：
- `Dict[str, Any]`: 工具执行结果，必须包含 `correlation_id` 字段

**可注入依赖点**：
- `get_deps` 回调支持延迟获取 `GatewayDepsProtocol`
- Handler 实现通过延迟导入获取

**工具路由映射**：

| 工具名 | Handler 函数 | 必需参数 |
|--------|-------------|----------|
| `memory_store` | `memory_store_impl` | `payload_md` |
| `memory_query` | `memory_query_impl` | `query` |
| `reliability_report` | `get_reliability_report` | (无) |
| `governance_update` | `governance_update_impl` | (无) |
| `evidence_upload` | `execute_evidence_upload` | `content`, `content_type` |

#### 5.4.2 list_tools

列出所有可用工具。

| 项目 | 说明 |
|------|------|
| **位置** | `src/engram/gateway/entrypoints/tool_executor.py:195-219` |
| **签名** | `list_tools() -> list` |
| **输出** | 工具定义列表 (按 name 字母顺序排序) |

#### 5.4.3 DefaultToolExecutor 类

实现 `ToolExecutorPort` 协议的默认工具执行器。

| 方法 | 签名 | 说明 |
|------|------|------|
| `list_tools` | `() -> List[Dict]` | 列出可用工具 |
| `call_tool` | `(name, arguments, context) -> ToolCallResult` | 执行工具调用 |
| `_validate_tool_params` | `(name, arguments) -> ToolCallResult \| None` | 校验工具参数 |

### 5.5 中间件层 (middleware.py)

#### 5.5.1 CorrelationIdMiddleware

统一处理 correlation_id 生成与传递的中间件。

| 项目 | 说明 |
|------|------|
| **位置** | `src/engram/gateway/middleware.py:88-127` |
| **职责** | 1. 生成 correlation_id 2. 存入 contextvars 3. 添加响应 header |

**相关函数**：
- `get_request_correlation_id()`: 获取当前请求的 correlation_id
- `set_request_correlation_id(correlation_id)`: 设置 correlation_id
- `reset_request_correlation_id_for_testing()`: 测试用重置函数

**测试覆盖**：
- `tests/gateway/test_mcp_jsonrpc_contract.py::TestCorrelationIdSingleSourceContract`
- `tests/gateway/test_mcp_jsonrpc_contract.py::TestCorrelationIdUnifiedContract`

### 5.6 公共 API 导出 (public_api.py)

| 导出项 | 来源 | 说明 |
|--------|------|------|
| `execute_tool` | `entrypoints.tool_executor` | MCP 工具执行入口 |
| `GatewayDeps` | `di` | 依赖容器实现类 |
| `GatewayDepsProtocol` | `di` | 依赖容器 Protocol |
| `RequestContext` | `di` | 请求上下文 |
| `LogbookAdapter` | `logbook_adapter` | Logbook 适配器 |

### 5.7 辅助函数汇总

| 函数 | 位置 | 说明 |
|------|------|------|
| `is_jsonrpc_request(body)` | mcp_rpc.py:513-515 | 判断是否为 JSON-RPC 格式 |
| `parse_jsonrpc_request(body)` | mcp_rpc.py:518-540 | 解析 JSON-RPC 请求 |
| `to_jsonrpc_error(error, req_id, tool_name, correlation_id)` | mcp_rpc.py:1027-1181 | 异常转换为 JSON-RPC 错误响应 |
| `format_tool_result(result)` | mcp_rpc.py:828-838 | 格式化工具结果为 MCP 格式 |
| `make_jsonrpc_error(id, code, message, data)` | mcp_rpc.py:490-502 | 构造 JSON-RPC 错误响应 |
| `make_jsonrpc_result(id, result)` | mcp_rpc.py:505-507 | 构造 JSON-RPC 成功响应 |
| `register_tool_executor(executor)` | mcp_rpc.py:1350-1363 | 注册工具执行器 |
| `get_tool_executor()` | mcp_rpc.py:1366-1368 | 获取工具执行器 |
| `generate_correlation_id()` | mcp_rpc.py:298-300 | 生成 correlation_id |
| `normalize_correlation_id(correlation_id)` | mcp_rpc.py:325-351 | 归一化 correlation_id |

### 5.8 测试覆盖索引

| 测试类/函数 | 覆盖组件 | 文件 |
|-------------|----------|------|
| `TestJsonRpcInvalidRequest` | JsonRpcRouter.dispatch | test_mcp_jsonrpc_contract.py |
| `TestToolsList` | handle_tools_list, list_tools | test_mcp_jsonrpc_contract.py |
| `TestToolsCallErrorAlignment` | handle_tools_call, _validate_tools_call_params | test_mcp_jsonrpc_contract.py |
| `TestErrorDataStructure` | ErrorData, to_jsonrpc_error | test_mcp_jsonrpc_contract.py |
| `TestCorrelationIdHeaderAlignment` | mcp_endpoint, CorrelationIdMiddleware | test_mcp_jsonrpc_contract.py |
| `TestCorrelationIdSingleSourceContract` | dispatch, get_current_correlation_id | test_mcp_jsonrpc_contract.py |
| `TestLegacyProtocolComplete` | mcp_endpoint 旧协议分支 | test_mcp_jsonrpc_contract.py |
| `TestErrorDataContractCompliance` | to_jsonrpc_error, ErrorData | test_mcp_jsonrpc_contract.py |
| `TestErrorReasonWhitelistConsistency` | ErrorReason 常量 | test_mcp_jsonrpc_contract.py |
| `TestCorrelationIdUnifiedContract` | correlation_id 全链路 | test_mcp_jsonrpc_contract.py |

---

## 6. 跨域契约关联

### 5.1 correlation_id 全链路一致性

```
MCP 入口 → 生成 correlation_id
    ↓
Handler 执行 → 传递 correlation_id
    ↓
审计写入 → 记录 correlation_id (gateway_event 内 + 顶层)
    ↓
Outbox 入队 → 关联 correlation_id
    ↓
Outbox Worker → 沿用原 correlation_id
    ↓
Reconcile → 通过 correlation_id 追溯完整链路
```

**测试锚点**：
- `tests/gateway/test_mcp_jsonrpc_contract.py::TestCorrelationIdSingleSourceContract`
- `tests/gateway/test_audit_event_contract.py::TestEvidenceRefsJsonCorrelationIdConsistencyContract`

### 5.2 deferred 响应与审计 redirect 对应关系

| 对外响应 | 审计 action | 审计 intended_action | 说明 |
|----------|-------------|---------------------|------|
| `action=allow` | `allow` | - | 直接成功 |
| `action=redirect` | `redirect` | - | 策略降级空间后成功 |
| `action=deferred` | `redirect` | `deferred` | OpenMemory 失败，入队 outbox |
| `action=reject` | `reject` | - | 策略拒绝 |

**测试锚点**：
- `tests/gateway/test_error_codes.py::TestDeferredOutboxScenarios`
- `tests/gateway/test_audit_event_contract.py::TestOpenMemoryFailureAuditEventSchema`

### 5.3 Reliability Report 统计口径

| 指标 | 计算公式 | 测试锚点 |
|------|----------|----------|
| `total` | `COUNT(*)` FROM write_audit | `test_reliability_report_contract.py` |
| `success_rate` | `success / (total - pending) * 100` | `TestReliabilityReportADRInvariants` |
| `redirect_outbox_closure` | `redirected 审计数 == outbox 总数` | `test_invariant_redirect_outbox_closure` |

---

## 7. 测试锚点索引

### 7.1 MCP JSON-RPC 测试

| 测试类 | 覆盖契约 |
|--------|----------|
| `TestJsonRpcInvalidRequest` | MCP-INV-01 |
| `TestToolsList` | MCP-INV-02 |
| `TestToolsCallErrorAlignment` | MCP-INV-03 |
| `TestErrorDataStructure` | MCP-INV-04 |
| `TestCorrelationIdHeaderAlignment` | MCP-INV-05 |
| `TestCorrelationIdSingleSourceContract` | MCP-INV-06 |
| `TestLegacyProtocolComplete` | MCP-INV-07 |
| `TestErrorDataContractCompliance` | MCP-INV-08 |
| `TestCorrelationIdUnifiedContract` | MCP-INV-05, MCP-INV-06 |
| `TestErrorDataFields` | MCP-INV-04 |
| `TestErrorReasonWhitelistConsistency` | MCP-INV-09 |

### 7.2 AuditEvent 测试

| 测试类 | 覆盖契约 |
|--------|----------|
| `TestSchemaVersionGuardrail` | AUDIT-INV-01 |
| `TestSourceEnum` | AUDIT-INV-02 |
| `TestEventTsFormat` | AUDIT-INV-03 |
| `TestDecisionSubstructure` | AUDIT-INV-04 |
| `TestEvidenceSummarySubstructure` | AUDIT-INV-05 |
| `TestEvidenceRefsJsonLogbookQueryContract` | AUDIT-INV-06 |
| `TestAuditFirstSemantics` | AUDIT-INV-07 |
| `TestOpenMemoryFailureAuditEventSchema` | AUDIT-INV-08 |
| `TestPolicySubstructure` | AUDIT-INV-09 |
| `TestEvidenceRefsJsonCorrelationIdConsistencyContract` | AUDIT-INV-10 |

### 7.3 Outbox & Reconcile 测试

| 测试类 | 覆盖契约 |
|--------|----------|
| `TestProcessResults` | OUTBOX-INV-01 |
| `TestReconcileSentRecords` | OUTBOX-INV-02 |
| `TestReconcileDeadRecords` | OUTBOX-INV-03 |
| `TestReconcileStaleRecords` | OUTBOX-INV-04 |
| `TestAuditOutboxInvariants` | OUTBOX-INV-05 |
| `TestLeaseProtocolCalls` | OUTBOX-INV-06 |
| `TestDeduplication` | OUTBOX-INV-07 |
| `TestReconcileSmokeTest` | OUTBOX-INV-08 |
| `TestReconcilePendingAuditTimeouts` | OUTBOX-INV-09 |
| `TestIdempotentFinalizeAudit` | OUTBOX-INV-10, OUTBOX-INV-12 |
| `TestReconcileReasonErrorCodeContract` | OUTBOX-INV-02~04 |
| `TestTwoPhaseAuditProtocol` | OUTBOX-INV-11 |
| `TestSinglePhaseRejectAudit` | OUTBOX-INV-13 |
| `TestEvidenceRefsJsonSemanticAnchors` | ANCHOR-INV-01~05 |
| `TestFinalizeWritebackMemoryId` | ANCHOR-INV-03 |
| `TestOpenMemoryAPIErrorHandling` | §10.2.2 (api_4xx/5xx 错误分类处理) |

### 7.4 Error Codes 测试

| 测试类 | 覆盖契约 |
|--------|----------|
| `TestErrorCodeConstants` | ERR-INV-01 |
| `TestErrorCodeConsistency` | ERR-INV-02 |
| `TestGatewayErrorCodes` | ERR-INV-03 |
| `TestDeferredOutboxScenarios` | ERR-INV-04 |
| `TestCorrelationIdInErrorResponses` | ERR-INV-05 |
| `TestErrorDataCorrelationIdContract` | ERR-INV-06 |
| `TestReconcileReasonErrorCodeContract` | ERR-INV-07 |

---

## 8. 相关文档

| 文档 | 路径 |
|------|------|
| MCP JSON-RPC 错误模型契约 | [mcp_jsonrpc_error_v1.md](./mcp_jsonrpc_error_v1.md) |
| MCP 契约决策与版本策略 (ADR) | [mcp_jsonrpc_error_v1.md §13](./mcp_jsonrpc_error_v1.md#13-契约决策与版本策略-adr) |
| Gateway 能力边界 | [../gateway/07_capability_boundary.md](../gateway/07_capability_boundary.md) |
| Gateway 审计原子性 ADR | [../architecture/adr_gateway_audit_atomicity.md](../architecture/adr_gateway_audit_atomicity.md) |
| Gateway Logbook 边界契约 | [gateway_logbook_boundary.md](./gateway_logbook_boundary.md) |
| 审计/证据/关联性契约 | [gateway_audit_evidence_correlation_contract.md](./gateway_audit_evidence_correlation_contract.md) |
| Outbox Lease 契约 | [outbox_lease_v1.md](./outbox_lease_v1.md) |

---

---

## 9. SSOT 指南：代码/测试/Schema 权威来源关系

> 本章节定义 Gateway 各契约的单一事实来源（SSOT），明确代码、测试、文档、Schema 之间的权威关系。

### 9.1 权威来源层级

```
           ┌─────────────────┐
           │   JSON Schema   │  ← 机器可读的结构定义
           │  (最高权威)      │
           └────────┬────────┘
                    │ 校验
           ┌────────▼────────┐
           │   代码实现       │  ← 运行时行为定义
           │  (行为权威)      │
           └────────┬────────┘
                    │ 验证
           ┌────────▼────────┐
           │   契约测试       │  ← 不变量守护
           │  (契约锚点)      │
           └────────┬────────┘
                    │ 描述
           ┌────────▼────────┐
           │   文档           │  ← 人类可读说明
           │  (最终派生)      │
           └─────────────────┘
```

### 9.2 各域 SSOT 定义

#### 9.2.1 MCP JSON-RPC 域

| 契约元素 | 权威来源 | 路径 | 说明 |
|----------|----------|------|------|
| ErrorData 结构 | JSON Schema | `schemas/mcp_jsonrpc_error_v1.schema.json` | 结构定义的最高权威 |
| ErrorCategory 枚举 | 代码 | `src/engram/gateway/mcp_rpc.py:ErrorCategory` | 运行时枚举定义 |
| ErrorReason 常量 | 代码 | `src/engram/gateway/mcp_rpc.py:ErrorReason` | 公开常量定义 |
| 错误码映射 | 代码 | `src/engram/gateway/mcp_rpc.py:JsonRpcCode` | -32xxx 错误码 |
| correlation_id 格式 | 测试 | `test_mcp_jsonrpc_contract.py::TestCorrelationIdHeaderAlignment` | 格式契约守护 |

**校验链**：
```
mcp_jsonrpc_error_v1.schema.json
    ↓ 被引用于
ErrorData (mcp_rpc.py)
    ↓ 被测试于
TestErrorDataSchemaValidation (test_mcp_jsonrpc_contract.py)
    ↓ 被描述于
mcp_jsonrpc_error_v1.md
```

#### 9.2.2 AuditEvent 域

| 契约元素 | 权威来源 | 路径 | 说明 |
|----------|----------|------|------|
| 审计事件结构 | JSON Schema | `schemas/audit_event_v1.schema.json` | 结构定义的最高权威 |
| source 枚举 | 代码 | `src/engram/gateway/services/audit_service.py` | 事件来源定义 |
| decision.action 枚举 | 代码 | `src/engram/gateway/policy.py` | allow/redirect/reject |
| evidence_refs_json 字段 | 测试 | `test_audit_event_contract.py::TestEvidenceRefsJsonLogbookQueryContract` | SQL 查询依赖字段 |

**校验链**：
```
audit_event_v1.schema.json
    ↓ 被引用于
AuditEvent 构造 (audit_service.py)
    ↓ 被测试于
TestAuditEventSchema (test_audit_event_contract.py)
    ↓ 被描述于
gateway_contract_convergence.md §2
```

#### 9.2.3 Outbox & Reconcile 域

| 契约元素 | 权威来源 | 路径 | 说明 |
|----------|----------|------|------|
| outbox 状态机 | 代码 | `src/engram/gateway/outbox_worker.py` | pending→sent/dead |
| ErrorCode 枚举 | 代码 | `src/engram/logbook/errors.py:ErrorCode` | Outbox reason 码 |
| audit/outbox 映射 | 文档 | `gateway_contract_convergence.md §3.2` | SSOT 映射表 |
| reconcile 退出码 | 测试 | `test_reconcile_outbox.py::TestReconcileSmokeTest` | 0/1/2 契约 |

**校验链**：
```
ErrorCode (errors.py)
    ↓ 被使用于
outbox_worker.py / reconcile_outbox.py
    ↓ 被测试于
TestReconcileReasonErrorCodeContract
    ↓ 被描述于
gateway_contract_convergence.md §3.2
```

#### 9.2.4 Error Codes 域

| 契约元素 | 权威来源 | 路径 | 说明 |
|----------|----------|------|------|
| JSON-RPC 错误码 | 代码 | `src/engram/gateway/mcp_rpc.py:JsonRpcCode` | -32xxx 常量 |
| ErrorReason 白名单 | 代码 | `src/engram/gateway/mcp_rpc.py:VALID_ERROR_REASONS` | 公开常量集合 |
| reason 命名规范 | 文档 | `gateway_contract_convergence.md §4.3` | 大写/小写规则 |

### 9.3 权威来源维护规则

| 规则 | 说明 |
|------|------|
| **Schema 先行** | 结构变更必须先更新 JSON Schema，再修改代码 |
| **测试守护** | 每条不变量必须有对应测试锚点，契约测试不得跳过 |
| **文档派生** | 文档描述必须与代码/Schema 一致，冲突时以代码/Schema 为准 |
| **禁止循环依赖** | 文档不得作为代码逻辑的判定依据 |

### 9.4 变更影响矩阵

当需要修改契约时，按以下顺序检查影响：

| 变更类型 | 影响顺序 | 必需操作 |
|----------|----------|----------|
| 新增可选字段 | Schema → 代码 → 测试 → 文档 | 更新 Schema，代码新增字段，测试验证，文档补充 |
| 修改字段语义 | 🚫 禁止 | 不得修改已发布字段的语义 |
| 新增枚举值 | 代码 → 测试 → Schema → 文档 | 代码新增常量，测试验证，Schema 更新枚举，文档补充 |
| 新增错误码 | 代码 → 测试 → 文档 | 代码新增常量，测试覆盖，文档补充 |
| 废弃字段 | 文档 → 代码 → 测试 → Schema | 文档标记废弃，代码保留至少 2 版本，测试添加废弃警告，最后从 Schema 移除 |

### 9.5 SSOT 文件索引

| 域 | 权威文件 | 作用 |
|----|----------|------|
| MCP 错误结构 | `schemas/mcp_jsonrpc_error_v1.schema.json` | ErrorData 结构定义 |
| 审计事件结构 | `schemas/audit_event_v1.schema.json` | AuditEvent 结构定义 |
| 可靠性报告结构 | `schemas/reliability_report_v1.schema.json` | ReliabilityReport 结构定义 |
| ErrorCode 枚举 | `src/engram/logbook/errors.py` | Outbox reason 码定义 |
| ErrorReason 枚举 | `src/engram/gateway/mcp_rpc.py` | JSON-RPC reason 码定义 |
| 契约测试 | `tests/gateway/test_*_contract.py` | 各域契约守护 |
| 契约文档 | `docs/contracts/*.md` | 人类可读契约说明 |

---

## 10. Gateway 路由策略（OpenMemory→Outbox）与错误语义

> **版本**: v1.0  
> **适用于**: `src/engram/gateway/handlers/memory_store.py`

本章定义 Gateway memory_store 请求的完整路由决策逻辑，包括决策输入、对外响应、对内审计状态转换规则。

### 10.1 核心术语定义

| 术语 | 定义 | 值域 |
|------|------|------|
| **policy_action** | 策略引擎返回的决策动作 | `allow` / `redirect` / `reject` |
| **intended_action** | 策略层面的原意动作，在 OpenMemory 失败入队时记录 | `allow` / `redirect` / `reject` |
| **response.action** | 对外响应的操作结果类型 | `allow` / `redirect` / `deferred` / `reject` / `error` |
| **audit.status** | 审计记录的最终状态 | `pending` / `success` / `redirected` / `failed` |

**intended_action 与 response.action 的关系**：

- `intended_action` = 策略引擎的原意决策（写入前的意图）
- `response.action` = 实际执行结果（可能因 OpenMemory 失败而降级为 `deferred`）

### 10.2 决策输入矩阵

#### 10.2.1 输入参数

| 输入 | 类型 | 说明 |
|------|------|------|
| `policy_action` | enum | 策略决策：`allow`（直接写入）/ `redirect`（重定向空间后写入）/ `reject`（拒绝） |
| `openmemory_result` | enum | OpenMemory 调用结果：`success` / `connection_error` / `api_4xx` / `api_5xx` / `generic_error` |
| `outbox_enqueue_result` | bool | Outbox 入队是否成功（仅在可恢复错误时触发） |
| `audit_write_result` | bool | 审计写入是否成功 |

#### 10.2.2 OpenMemory 异常分类

| 异常类型 | 对应代码异常 | 可恢复性 | 处理策略 |
|----------|-------------|----------|----------|
| `connection` | `OpenMemoryConnectionError` | ✅ 可恢复 | 入队 outbox 重试 |
| `api_4xx` | `OpenMemoryAPIError (400-499)` | ❌ 不可恢复 | 直接返回错误，不入队 |
| `api_5xx` | `OpenMemoryAPIError (500+)` | ✅ 可恢复 | 入队 outbox 重试 |
| `generic` | `OpenMemoryError` | ✅ 可恢复 | 入队 outbox 重试 |

**代码引用**：

```python
# src/engram/gateway/handlers/memory_store.py
except OpenMemoryConnectionError as e:
    # 连接/超时错误：可恢复，入队 outbox 重试
    return _handle_openmemory_failure(...)

except OpenMemoryAPIError as e:
    if e.status_code is not None and 400 <= e.status_code < 500:
        # 4xx 客户端错误：不可恢复，直接返回错误
        return _handle_client_error(...)
    else:
        # 5xx 服务端错误：可恢复，入队 outbox 重试
        return _handle_openmemory_failure(...)
```

### 10.3 对外响应：MemoryStoreResponse

#### 10.3.1 响应字段定义

| 字段 | 类型 | 必需性 | 说明 |
|------|------|--------|------|
| `ok` | `bool` | 必需 | 操作是否成功（`true`: 成功或已入队，`false`: 失败） |
| `action` | `string` | 必需 | 操作结果类型（见下表） |
| `space_written` | `string` | 条件必需 | 实际写入的空间（`action=allow/redirect` 时必需） |
| `memory_id` | `string` | 条件必需 | OpenMemory 记录 ID（`action=allow/redirect` 时必需） |
| `outbox_id` | `int` | 条件必需 | Outbox 队列 ID（`action=deferred` 时必需） |
| `correlation_id` | `string` | 必需 | 请求追踪 ID |
| `message` | `string` | 可选 | 错误或提示信息 |

#### 10.3.2 response.action 枚举定义

| action | ok | 语义 | 触发场景 |
|--------|-----|------|----------|
| `allow` | `true` | 直接写入成功 | 策略允许 + OpenMemory 成功 |
| `redirect` | `true` | 空间重定向后写入成功 | 策略重定向 + OpenMemory 成功 |
| `deferred` | `false` | 写入已入队 outbox | OpenMemory 可恢复错误 |
| `reject` | `false` | 策略拒绝 | 策略返回 reject |
| `error` | `false` | 系统错误 | 审计写入失败 / OpenMemory 4xx / 未预期异常 |

### 10.4 路由决策矩阵（完整）

| policy_action | OpenMemory 结果 | Outbox 入队 | response.action | audit.status | intended_action |
|---------------|-----------------|-------------|-----------------|--------------|-----------------|
| `reject` | - | - | `reject` | `success` (单阶段) | - |
| `allow` | `success` | - | `allow` | `success` | - |
| `redirect` | `success` | - | `redirect` | `success` | - |
| `allow` | `connection` | ✅ 成功 | `deferred` | `redirected` | `allow` |
| `redirect` | `connection` | ✅ 成功 | `deferred` | `redirected` | `redirect` |
| `allow` | `api_5xx` | ✅ 成功 | `deferred` | `redirected` | `allow` |
| `redirect` | `api_5xx` | ✅ 成功 | `deferred` | `redirected` | `redirect` |
| `allow` | `generic` | ✅ 成功 | `deferred` | `redirected` | `allow` |
| `redirect` | `generic` | ✅ 成功 | `deferred` | `redirected` | `redirect` |
| `allow`/`redirect` | `api_4xx` | - (不入队) | `error` | `failed` | - |
| - | - | - | `error` | - | - | (审计写入失败) |

**关键约束**：

1. **reject 单阶段**：`policy_action=reject` 不产生 pending 审计，直接写入 `status=success` 的最终审计
2. **4xx 不入队**：OpenMemory 4xx 错误是不可恢复的客户端错误，不入队 outbox
3. **intended_action 仅 deferred 场景**：仅当 `response.action=deferred` 时写入 `intended_action`

### 10.5 对内审计：状态转换规则

#### 10.5.1 两阶段审计协议状态机

```
                    ┌─────────────┐
                    │   (初始)     │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
              ▼            ▼            ▼
        ┌─────────┐  ┌──────────┐  ┌─────────┐
        │ pending │  │ success  │  │ (单阶段) │
        │(两阶段) │  │(直接写入)│  │  reject │
        └────┬────┘  └──────────┘  └─────────┘
             │
    ┌────────┼────────┬────────────┐
    │        │        │            │
    ▼        ▼        ▼            ▼
┌───────┐┌──────────┐┌─────────┐┌─────────┐
│success││redirected││ failed  ││(timeout)│
│(成功) ││(入队OB)  ││(4xx/err)││ failed  │
└───────┘└──────────┘└─────────┘└─────────┘
```

#### 10.5.2 finalize 状态转换规则

| 触发场景 | pending → | reason_suffix | evidence_refs_json_patch |
|----------|-----------|---------------|--------------------------|
| OpenMemory 成功 | `success` | (无) | `{"memory_id": "<id>"}` |
| OpenMemory 可恢复错误 → 入队 | `redirected` | `:outbox:<id>` | `{"outbox_id": <id>, "intended_action": "<action>"}` |
| OpenMemory 4xx 错误 | `failed` | `:client_error:<status>` | `{"error_type": "client_error", "status_code": <n>, "error_message": "..."}` |
| 超时清理 (reconcile) | `failed` | `:timeout` | `{"timeout_detected_at": "...", "reconcile_action": "mark_failed_timeout"}` |

#### 10.5.3 reason_suffix 兼容策略

> **设计决策**：`reason_suffix` 保留用于向后兼容，但新代码应通过 `evidence_refs_json` 顶层字段进行跨阶段查询。

| 场景 | reason_suffix 格式 | 推荐查询方式 |
|------|-------------------|--------------|
| 入队 outbox | `:outbox:<outbox_id>` | `evidence_refs_json->>'outbox_id'` |
| 4xx 客户端错误 | `:client_error:<status_code>` | `evidence_refs_json->>'error_type'` |
| 超时清理 | `:timeout` | `evidence_refs_json->>'reconcile_action'` |

**SQL 查询示例**：

```sql
-- 推荐：通过 evidence_refs_json 顶层字段查询
SELECT * FROM governance.write_audit
WHERE (evidence_refs_json->>'outbox_id')::int = 12345;

-- 兼容：通过 reason 后缀查询（不推荐新代码使用）
SELECT * FROM governance.write_audit
WHERE reason LIKE '%:outbox:12345';
```

#### 10.5.4 evidence_refs_json_patch 字段约定

| 场景 | 写入字段 | 数据类型 | 说明 |
|------|----------|----------|------|
| **成功** | `memory_id` | `string` | OpenMemory 返回的记忆标识 |
| **入队 outbox** | `outbox_id` | `integer` | `logbook.outbox_memory` 表主键 |
| | `intended_action` | `string` | 策略原意动作 (`allow`/`redirect`) |
| **4xx 错误** | `error_type` | `string` | 固定为 `"client_error"` |
| | `status_code` | `integer` | HTTP 状态码 (400-499) |
| | `error_message` | `string` | 错误详情 |
| **超时清理** | `timeout_detected_at` | `string` | ISO 8601 时间戳 |
| | `reconcile_action` | `string` | 固定为 `"mark_failed_timeout"` |

### 10.6 intended_action 字段规范

#### 10.6.1 定义

| 属性 | 定义 |
|------|------|
| **语义** | 策略引擎的原意决策动作，记录"本应执行但因故降级"的意图 |
| **值域** | `"allow"` \| `"redirect"` \| `"reject"` |
| **写入时机** | OpenMemory 写入失败入队 outbox 时（finalize 阶段） |
| **位置** | `evidence_refs_json.intended_action` (顶层) |
| **代码引用** | `src/engram/gateway/handlers/memory_store.py:_handle_openmemory_failure` |

#### 10.6.2 使用场景

**场景 A**：策略决定 `allow`，但 OpenMemory 失败导致响应为 `deferred`

```json
{
  "response": {
    "ok": false,
    "action": "deferred",
    "outbox_id": 12345
  },
  "audit": {
    "status": "redirected",
    "evidence_refs_json": {
      "outbox_id": 12345,
      "intended_action": "allow"
    }
  }
}
```

**场景 B**：策略决定 `redirect`，但 OpenMemory 失败导致响应为 `deferred`

```json
{
  "response": {
    "ok": false,
    "action": "deferred",
    "outbox_id": 12346
  },
  "audit": {
    "status": "redirected",
    "evidence_refs_json": {
      "outbox_id": 12346,
      "intended_action": "redirect"
    }
  }
}
```

#### 10.6.3 与 conflict_intended_operation 的区别

| 字段 | 所属层 | 值域 | 用途 |
|------|--------|------|------|
| `intended_action` | 策略层 (Gateway) | `allow`/`redirect`/`reject` | 追踪策略原意 |
| `conflict_intended_operation` | Worker 层 (Outbox) | `success`/`retry`/`dead`/... | 诊断 lease 冲突 |

### 10.7 不变量

| 编号 | 不变量 | 说明 | 测试锚点 |
|------|--------|------|----------|
| ROUTE-INV-01 | reject 不产生 pending | 单阶段审计，直接写入最终状态 | `test_audit_event_contract.py::TestSinglePhaseRejectAudit` |
| ROUTE-INV-02 | 4xx 错误不入队 outbox | 不可恢复错误，finalize 为 failed | `test_memory_store.py::TestClientErrorHandling` |
| ROUTE-INV-03 | deferred 必须有 outbox_id | response.action=deferred 时 outbox_id 必填 | `test_error_codes.py::TestDeferredOutboxScenarios` |
| ROUTE-INV-04 | intended_action 仅 deferred 场景 | 仅在入队 outbox 时写入 | `test_audit_event_contract.py::test_intended_action_at_top_level_for_redirect_deferred` |
| ROUTE-INV-05 | reason_suffix 与 patch 一致 | `:outbox:<id>` 与 `{"outbox_id": <id>}` 必须一致 | `test_audit_event_contract.py::TestReasonSuffixConsistency` |

### 10.8 相关文档

| 文档 | 说明 |
|------|------|
| [ADR: Gateway 审计原子性](../architecture/adr_gateway_audit_atomicity.md) | OpenMemory 错误分类规则 |
| [Gateway 审计/证据/关联性契约](./gateway_audit_evidence_correlation_contract.md) | evidence_refs_json_patch 详细规范 |
| [Gateway 能力边界](../gateway/07_capability_boundary.md) | MemoryStoreResponse 响应契约 |

---

## 11. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-02-01 | 初始版本，汇总四域契约 |
| v1.1 | 2026-02-01 | 新增第5章 MCP 工具路由实现详解 |
| v1.2 | 2026-02-01 | 新增第9章 SSOT 指南：代码/测试/Schema 权威来源关系 |
| v1.3 | 2026-02-01 | 新增 §3.1.1 两阶段审计协议详解（pending 前置条件、finalize 幂等约束、patch 合并策略）；新增 §3.1.2 单阶段 reject 审计协议；新增 §2.1.2 语义锚点列表（correlation_id/outbox_id/memory_id/payload_sha/source/extra）及 SQL 示例 |
| v1.4 | 2026-02-01 | 新增 §4.6.1 -32000 废弃决策；更新 §8 相关文档索引添加契约决策 ADR 引用 |
| v1.5 | 2026-02-01 | 新增第10章 Gateway 路由策略（OpenMemory→Outbox）与错误语义，包含决策输入矩阵、MemoryStoreResponse 响应契约、finalize 状态转换规则、intended_action 字段规范 |
