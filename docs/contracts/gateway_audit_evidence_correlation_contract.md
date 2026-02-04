# Gateway 审计/证据/关联性端到端契约

> **版本**: v1.0  
> **创建日期**: 2026-02-01  
> **状态**: 生效中

---

## 1. 术语表

本文档使用以下术语，必须理解其含义以正确实现和维护审计链路。

| 术语 | 定义 | 生成位置 | 格式 |
|------|------|----------|------|
| **request correlation_id** | 单次 HTTP 请求的追踪标识，用于关联同一请求的所有审计记录、日志和响应 | HTTP 入口层 (`app.py`) | `corr-{16位十六进制}` |
| **batch correlation_id** | Outbox Worker 一次批处理的追踪标识，用于关联同一批次的所有 outbox 记录处理 | `outbox_worker.py:process_batch()` | `corr-{16位十六进制}` |
| **attempt_id** | 单条 outbox 记录的单次处理尝试标识，用于冲突检测和重试追踪 | `outbox_worker.py:process_single_item()` | `attempt-{12位十六进制}` |
| **outbox_id** | 数据库表 `logbook.outbox_memory` 的主键，用于跨阶段（同步→异步）关联 | PostgreSQL 自动生成 | 整数 |

### 1.1 术语关系图

```
┌─────────────────────────────────────────────────────────────────────┐
│                     同步请求阶段                                      │
│   correlation_id = "corr-a1b2c3d4e5f67890"                          │
│                                                                     │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐            │
│   │ HTTP 入口    │ →  │ 业务处理    │ →  │ 审计写入    │            │
│   │ (app.py)    │    │ (handlers)  │    │ (write_audit)│            │
│   └─────────────┘    └─────────────┘    └──────┬──────┘            │
│                                                 │                   │
│                           降级时产生 outbox_id: 12345               │
└─────────────────────────────────────────────────┼───────────────────┘
                                                  │
                                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     异步处理阶段                                      │
│   batch correlation_id = "corr-fedcba0987654321"  ← 新生成          │
│                                                                     │
│   ┌─────────────┐    outbox_id: 12345    ┌─────────────┐           │
│   │ Outbox Worker│ ─────────────────────→ │ 审计写入    │           │
│   │ attempt_id:  │                        │ (reason:    │           │
│   │ attempt-abc12│                        │ outbox_flush│           │
│   └─────────────┘                        │ _success)   │           │
│                                          └─────────────┘           │
└─────────────────────────────────────────────────────────────────────┘
```

**关键约束**：

- **同步阶段**：同一 HTTP 请求的所有操作使用相同的 `correlation_id`
- **异步阶段**：`correlation_id` 可以不同（每批次新生成），通过 `outbox_id` 与同步阶段关联
- **跨阶段追踪**：使用 `outbox_id` 作为桥梁，而非 `correlation_id`

---

## 2. 同步链路：correlation_id 生成与传播

### 2.1 生成点（单一来源）

**correlation_id 只在 HTTP 入口层生成一次**，生成后透传到所有子调用。

#### 代码引用：`src/engram/gateway/app.py`

```python
# /mcp 端点入口（JSON-RPC 和旧协议）
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    correlation_id = generate_correlation_id()  # ← 唯一生成点
    # ...
    response = await mcp_router.dispatch(rpc_request, correlation_id=correlation_id)

# /memory/store REST 端点
@app.post("/memory/store")
async def memory_store_endpoint(request: MemoryStoreRequest):
    correlation_id = generate_correlation_id()  # ← 唯一生成点
    return await memory_store_impl(..., correlation_id=correlation_id, deps=deps)

# /memory/query REST 端点
@app.post("/memory/query")
async def memory_query_endpoint(request: MemoryQueryRequest):
    correlation_id = generate_correlation_id()  # ← 唯一生成点
    return await memory_query_impl(..., correlation_id=correlation_id, deps=deps)
```

#### 代码引用：`src/engram/gateway/mcp_rpc.py`

```python
def generate_correlation_id() -> str:
    """生成关联 ID"""
    return f"corr-{uuid.uuid4().hex[:16]}"

# correlation_id 格式校验正则表达式（与 schemas/audit_event_v2.schema.json 对齐）
CORRELATION_ID_PATTERN = re.compile(r"^corr-[a-fA-F0-9]{16}$")
```

### 2.2 传播路径

#### 2.2.1 /mcp JSON-RPC 链路

```
HTTP 入口 (app.py:mcp_endpoint)
    │
    ├─ correlation_id = generate_correlation_id()
    │
    └─→ mcp_router.dispatch(rpc_request, correlation_id=correlation_id)
            │
            ├─ set_current_correlation_id(correlation_id)  [contextvars]
            │
            └─→ handle_tools_call(params)
                    │
                    ├─ correlation_id = get_current_correlation_id()
                    │
                    └─→ executor(tool_name, args, correlation_id=correlation_id)
                            │
                            └─→ memory_store_impl(..., correlation_id=correlation_id)
                                    │
                                    └─→ 审计写入: evidence_refs_json.correlation_id
```

#### 2.2.2 /mcp 旧协议链路

```
HTTP 入口 (app.py:mcp_endpoint)
    │
    ├─ correlation_id = generate_correlation_id()
    │
    └─→ _execute_tool(tool, args, correlation_id)
            │
            └─→ memory_store_impl(..., correlation_id=correlation_id)
                    │
                    └─→ 审计写入: evidence_refs_json.correlation_id
```

#### 2.2.3 /memory/store REST 链路

```
HTTP 入口 (app.py:memory_store_endpoint)
    │
    ├─ correlation_id = generate_correlation_id()
    │
    └─→ memory_store_impl(..., correlation_id=correlation_id)
            │
            └─→ 审计写入: evidence_refs_json.correlation_id
```

#### 2.2.4 /memory/query REST 链路

```
HTTP 入口 (app.py:memory_query_endpoint)
    │
    ├─ correlation_id = generate_correlation_id()
    │
    └─→ memory_query_impl(..., correlation_id=correlation_id)
```

### 2.3 contextvars 传递机制

`mcp_rpc.py` 使用 `contextvars` 在 `dispatch` → `handler` 之间传递 correlation_id：

```python
# mcp_rpc.py
_current_correlation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_current_correlation_id", default=None
)

def get_current_correlation_id() -> Optional[str]:
    """获取当前请求的 correlation_id"""
    return _current_correlation_id.get()

def set_current_correlation_id(correlation_id: Optional[str]) -> contextvars.Token:
    """设置当前请求的 correlation_id"""
    return _current_correlation_id.set(correlation_id)

# dispatch() 中使用
async def dispatch(self, request, correlation_id=None):
    corr_id = normalize_correlation_id(correlation_id)
    token = set_current_correlation_id(corr_id)
    try:
        result = await handler(params)
        return make_jsonrpc_result(req_id, result)
    finally:
        _current_correlation_id.reset(token)
```

---

## 3. 错误契约

### 3.1 error.data.correlation_id 一致性契约

**契约要求**：JSON-RPC 错误响应中的 `error.data.correlation_id` 必须等于入口层生成的 `correlation_id`。

#### 错误响应结构

```json
{
    "jsonrpc": "2.0",
    "id": 1,
    "error": {
        "code": -32001,
        "message": "依赖服务不可用: OpenMemory ...",
        "data": {
            "category": "dependency",
            "reason": "OPENMEMORY_UNAVAILABLE",
            "retryable": true,
            "correlation_id": "corr-a1b2c3d4e5f67890",  // ← 必须与入口一致
            "details": {...}
        }
    }
}
```

#### 实现保证（app.py）

```python
# JSON-RPC 分支：确保 error.data 中有 correlation_id
if response.error and response.error.data:
    if isinstance(response.error.data, dict) and "correlation_id" not in response.error.data:
        response.error.data["correlation_id"] = correlation_id

# 旧协议分支：错误响应始终包含 correlation_id
return JSONResponse(
    content={"ok": False, "error": str(e), "correlation_id": correlation_id},
    headers=response_headers,
)
```

### 3.2 ErrorData 归一化

`ErrorData.to_dict()` 会自动归一化 correlation_id，确保格式符合 schema：

```python
def to_dict(self) -> Dict[str, Any]:
    # 归一化 correlation_id（确保符合 schema 格式: corr-{16位十六进制}）
    corr_id = normalize_correlation_id(self.correlation_id)
    return {
        "category": self.category,
        "reason": self.reason,
        "retryable": self.retryable,
        "correlation_id": corr_id,
        # ...
    }
```

### 3.3 响应头契约

HTTP 响应头 `X-Correlation-ID` 必须与业务响应中的 `correlation_id` 一致：

```python
def _make_cors_headers_with_correlation_id(correlation_id: str) -> dict:
    """
    契约：X-Correlation-ID 必须与业务响应中的 correlation_id 一致（单次生成语义）
    """
    return {
        **MCP_CORS_HEADERS,
        "X-Correlation-ID": correlation_id,
    }
```

---

## 4. 异步链路：Outbox Worker / Reconcile

### 4.1 设计原则

异步链路（Outbox Worker、Reconcile）的 correlation_id **允许与同步链路不同**，跨阶段使用 `outbox_id` 关联。

**原因**：

1. 异步处理发生在不同的进程/时间
2. 同一 outbox 记录可能被多次重试，每次需要独立追踪
3. 批量处理需要批次级别的追踪标识

### 4.2 Outbox Worker 链路

#### 代码引用：`src/engram/gateway/outbox_worker.py`

```python
def process_batch(config: WorkerConfig, worker_id: Optional[str] = None) -> list[ProcessResult]:
    # 生成批次级 correlation_id
    correlation_id = f"corr-{uuid.uuid4().hex[:16]}"
    
    for item in items:
        # 为每条记录生成唯一 attempt_id
        attempt_id = f"attempt-{uuid.uuid4().hex[:12]}"
        result = process_single_item(
            item, worker_id, client, config,
            attempt_id=attempt_id,
            correlation_id=correlation_id,  # ← 批次级 correlation_id
        )

def process_single_item(..., correlation_id: Optional[str] = None):
    # 审计写入时包含 outbox_id 用于跨阶段关联
    event = build_outbox_worker_audit_event(
        operation="outbox_flush",
        correlation_id=correlation_id,      # ← 批次级（与同步阶段不同）
        outbox_id=outbox_id,                # ← 跨阶段关联键
        worker_id=worker_id,
        attempt_id=attempt_id,
        # ...
    )
```

### 4.3 Reconcile Outbox 链路

#### 代码引用：`src/engram/gateway/reconcile_outbox.py`

Reconcile 模块补写审计时，同样使用 `outbox_id` 作为关联键：

```python
def write_reconcile_audit(outbox: Dict[str, Any], reason: str, action: str, ...):
    gateway_event = build_reconcile_audit_event(
        operation="outbox_reconcile",
        # correlation_id 可选（由 build_reconcile_audit_event 自动生成）
        outbox_id=outbox["outbox_id"],  # ← 跨阶段关联键
        # ...
    )
```

### 4.4 跨阶段追踪查询

使用 `outbox_id` 而非 `correlation_id` 进行跨阶段查询：

```sql
-- 查询某 outbox 记录的所有审计历史
SELECT * FROM governance.write_audit
WHERE (evidence_refs_json->>'outbox_id')::int = :outbox_id
ORDER BY created_at;

-- 查询同步阶段审计（reason 不含 outbox_flush）
SELECT * FROM governance.write_audit
WHERE (evidence_refs_json->>'outbox_id')::int = :outbox_id
  AND reason NOT LIKE 'outbox_flush%';

-- 查询异步阶段审计（reason 含 outbox_flush）
SELECT * FROM governance.write_audit
WHERE (evidence_refs_json->>'outbox_id')::int = :outbox_id
  AND reason LIKE 'outbox_flush%';
```

---

## 5. evidence_refs_json 结构与 SQL 依赖

### 5.1 顶层结构定义

`evidence_refs_json` 是写入 `governance.write_audit.evidence_refs_json` 列的 JSON 结构，用于审计追踪和对账查询。

```json
{
    "gateway_event": {
        "schema_version": "1.1",
        "source": "gateway",
        "operation": "memory_store",
        "correlation_id": "corr-a1b2c3d4e5f67890",
        "actor_user_id": "user1",
        "decision": {"action": "allow", "reason": "policy_passed"},
        // ... 其他字段
    },
    "patches": [...],
    "attachments": [...],
    "external": [...],
    "evidence_summary": {"count": 2, "has_strong": true, "uris": [...]}
}
```

### 5.2 顶层字段不可移除契约

以下字段被对账查询依赖，**禁止移除或重命名**：

| 字段路径 | 用途 | 依赖的 SQL 查询 |
|----------|------|----------------|
| `evidence_refs_json->>'outbox_id'` | 定位 outbox 关联审计记录 | `WHERE (evidence_refs_json->>'outbox_id')::int = ?` |
| `evidence_refs_json->>'source'` | 区分事件来源 | `WHERE evidence_refs_json->>'source' = 'outbox_worker'` |
| `evidence_refs_json->>'correlation_id'` | 追踪请求链路（顶层提升） | `evidence_refs_json->>'correlation_id'` |
| `evidence_refs_json->>'payload_sha'` | 内容去重/追踪 | Dedupe 查询 |
| `evidence_refs_json->>'memory_id'` | OpenMemory 记录关联 | 成功写入追踪 |
| `evidence_refs_json->>'retry_count'` | 重试次数追踪 | 可选，outbox_worker 审计 |
| `evidence_refs_json->>'next_attempt_at'` | 下次尝试时间 | 可选，outbox_worker 审计 |
| `evidence_refs_json->>'worker_id'` | Worker 标识 | 可选，从 extra 提升 |
| `evidence_refs_json->>'attempt_id'` | 尝试标识 | 可选，从 extra 提升 |
| `evidence_refs_json->>'intended_action'` | 原意动作 | 可选，redirect→deferred 场景 |
| `evidence_refs_json->'gateway_event'->'decision'->>'action'` | 统计决策分布 | 可靠性报告聚合 |
| `evidence_refs_json->'gateway_event'->'decision'->>'reason'` | 错误原因分析 | 故障排查查询 |

### 5.3 reconcile_outbox.py SQL 契约声明

> 引用自 `src/engram/gateway/reconcile_outbox.py` 模块 docstring

```
SQL 契约声明
============

本模块对 governance.write_audit.evidence_refs_json 的 SQL 查询依赖以下契约：

1. 顶层必需字段（用于 SQL 查询）
   --------------------------------
   - outbox_id   : int    — 查询: (evidence_refs_json->>'outbox_id')::int
   - source      : str    — 查询: evidence_refs_json->>'source'
   - correlation_id : str — 查询: evidence_refs_json->>'correlation_id'
   - payload_sha : str    — 查询: evidence_refs_json->>'payload_sha'
   - memory_id   : str    — 查询: evidence_refs_json->>'memory_id' (可选)
   - retry_count : int    — 查询: evidence_refs_json->>'retry_count' (可选)
   - intended_action : str — 查询: evidence_refs_json->>'intended_action' (redirect 场景)

2. reason/action 组合映射
   --------------------------------
   | outbox 状态      | 审计 reason                | 审计 action |
   |------------------|---------------------------|-------------|
   | sent             | outbox_flush_success      | allow       |
   | sent (dedup)     | outbox_flush_dedup_hit    | allow       |
   | dead             | outbox_flush_dead         | reject      |
   | pending (stale)  | outbox_stale              | redirect    |
```

### 5.4 evidence_refs_json_patch 写入时机

> **设计决策：方案 A（顶层字段后置合并）**
>
> 本项目选择 **方案 A**：通过 `finalize_audit` 的 `evidence_refs_json_patch` 参数在 finalize 阶段将 `outbox_id`/`memory_id`/`intended_action` 等跨阶段关联字段合并到 `evidence_refs_json` 顶层。
>
> **选择理由**：
> - **时序正确**：`outbox_id` 在 `enqueue_outbox` 后才生成，无法在 pre-audit 阶段写入
> - **幂等更新**：使用 `jsonb ||` 合并操作，多次执行结果一致
> - **SQL 查询友好**：顶层字段支持 `evidence_refs_json->>'outbox_id'` 直接查询
>
> **方案 B（pre-audit 携带 outbox_id）不适用**：因为 pre-audit 发生在 OpenMemory 调用之前，此时 outbox 尚未创建。

两阶段审计的 finalize 步骤支持通过 `evidence_refs_json_patch` 参数将跨阶段关联字段合并到 `evidence_refs_json` 顶层。

#### 代码引用：`src/engram/gateway/services/audit_service.py`

```python
def finalize_audit(
    db: Any,
    correlation_id: str,
    status: str,
    reason_suffix: Optional[str] = None,
    evidence_refs_json_patch: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    evidence_refs_json_patch 用于跨阶段关联查询，合并到 evidence_refs_json 顶层。
    """
```

#### 底层实现：`src/engram/logbook/governance.py:update_write_audit`

```python
# 使用 jsonb || 合并操作符将 patch 合并到 evidence_refs_json 顶层（幂等）
if evidence_refs_json_patch:
    set_clauses.append(
        "evidence_refs_json = COALESCE(evidence_refs_json, '{}'::jsonb) || %s::jsonb"
    )
    params.append(json.dumps(evidence_refs_json_patch))
```

**幂等性保证**：`jsonb ||` 合并操作是幂等的，重复调用会覆盖同名 key，不会产生重复或冲突。

#### 写入场景

| 场景 | 写入时机 | evidence_refs_json_patch 内容 |
|------|----------|------------------------------|
| OpenMemory 写入成功 | `_handle_success()` | `{"memory_id": "<memory_id>"}` |
| OpenMemory 写入失败（入队 outbox） | `_handle_openmemory_failure()` | `{"outbox_id": <id>, "intended_action": "<action>"}` |

#### 代码引用：`src/engram/gateway/handlers/memory_store.py`

```python
# _handle_success() - 成功场景
finalize_audit(
    db=db,
    correlation_id=correlation_id,
    status="success",
    evidence_refs_json_patch={"memory_id": memory_id},
)

# _handle_openmemory_failure() - 失败入队场景
finalize_audit(
    db=db,
    correlation_id=correlation_id,
    status="redirected",
    reason_suffix=f":outbox:{outbox_id}",  # 保留向后兼容
    evidence_refs_json_patch={"outbox_id": outbox_id, "intended_action": action},
)
```

#### 跨阶段查询示例

```sql
-- 通过 memory_id 查询成功写入的审计记录
SELECT * FROM governance.write_audit
WHERE evidence_refs_json->>'memory_id' = :memory_id;

-- 通过 outbox_id 查询跨阶段关联审计（推荐使用此方式，而非解析 reason 字段）
SELECT * FROM governance.write_audit
WHERE (evidence_refs_json->>'outbox_id')::int = :outbox_id;

-- 查询原意动作为 allow 但被降级的记录
SELECT * FROM governance.write_audit
WHERE evidence_refs_json->>'intended_action' = 'allow'
  AND status = 'redirected';
```

**注意**：`reason_suffix=:outbox:<id>` 格式仍保留用于向后兼容，但新代码应通过 `evidence_refs_json->>'outbox_id'` 进行跨阶段查询，不再依赖 reason 字段解析。

### 5.5 finalize 写回字段规范

> **目的**：明确 finalize 阶段写回 `evidence_refs_json` 顶层的字段定义、数据类型、写入时机及 SQL 查询依赖。

#### 5.5.1 字段定义

| 字段名 | 数据类型 | 写入时机 | 说明 |
|--------|----------|----------|------|
| `memory_id` | `string` | OpenMemory 写入成功后 | OpenMemory 返回的记忆标识，用于关联成功写入的记录 |
| `outbox_id` | `integer` | OpenMemory 写入失败入队后 | `logbook.outbox_memory` 表主键，用于跨阶段关联降级场景 |
| `intended_action` | `string` | OpenMemory 写入失败入队后 | 原策略决策的 action（如 `"allow"`/`"redirect"`），记录降级前的原意 |

#### 5.5.2 写入时机详解

**场景 A：OpenMemory 写入成功**

```python
# 代码引用：src/engram/gateway/handlers/memory_store.py:_handle_success()
finalize_audit(
    db=db,
    correlation_id=correlation_id,
    status="success",
    evidence_refs_json_patch={"memory_id": memory_id},  # ← 写回 memory_id
)
```

- **触发条件**：`OpenMemoryClient.store()` 返回成功且 `memory_id` 非空
- **写入字段**：`memory_id`
- **status 更新**：`pending` → `success`

**场景 B：OpenMemory 写入失败，入队 outbox**

```python
# 代码引用：src/engram/gateway/handlers/memory_store.py:_handle_openmemory_failure()
outbox_id = db.enqueue_outbox(...)  # ← 先入队获取 outbox_id

finalize_audit(
    db=db,
    correlation_id=correlation_id,
    status="redirected",
    reason_suffix=f":outbox:{outbox_id}",  # 向后兼容
    evidence_refs_json_patch={
        "outbox_id": outbox_id,           # ← 写回 outbox_id (integer)
        "intended_action": action,        # ← 写回原意 action (string)
    },
)
```

- **触发条件**：`OpenMemoryClient.store()` 抛出 `OpenMemoryConnectionError` 或 `OpenMemoryError`
- **写入字段**：`outbox_id`、`intended_action`
- **status 更新**：`pending` → `redirected`

#### 5.5.3 底层实现（SQL 合并操作）

```python
# 代码引用：src/engram/logbook/governance.py:update_write_audit()

if evidence_refs_json_patch:
    # 使用 jsonb || 合并操作符将 patch 合并到 evidence_refs_json 顶层
    set_clauses.append(
        "evidence_refs_json = COALESCE(evidence_refs_json, '{}'::jsonb) || %s::jsonb"
    )
    params.append(json.dumps(evidence_refs_json_patch))
```

**SQL 执行示例**：

```sql
-- 成功场景：写入 memory_id
UPDATE governance.write_audit
SET status = 'success',
    evidence_refs_json = COALESCE(evidence_refs_json, '{}'::jsonb) || '{"memory_id": "mem-abc123"}'::jsonb,
    updated_at = now()
WHERE correlation_id = 'corr-a1b2c3d4e5f67890'
  AND status = 'pending';

-- 降级场景：写入 outbox_id + intended_action
UPDATE governance.write_audit
SET status = 'redirected',
    reason = reason || ':outbox:12345',
    evidence_refs_json = COALESCE(evidence_refs_json, '{}'::jsonb) || '{"outbox_id": 12345, "intended_action": "allow"}'::jsonb,
    updated_at = now()
WHERE correlation_id = 'corr-a1b2c3d4e5f67890'
  AND status = 'pending';
```

#### 5.5.4 SQL 查询依赖

| 查询场景 | SQL 表达式 | 依赖字段 |
|----------|------------|----------|
| 通过 memory_id 查询成功写入 | `evidence_refs_json->>'memory_id' = :memory_id` | `memory_id` |
| 通过 outbox_id 跨阶段关联 | `(evidence_refs_json->>'outbox_id')::int = :outbox_id` | `outbox_id` |
| 查询原意被降级的记录 | `evidence_refs_json->>'intended_action' = 'allow' AND status = 'redirected'` | `intended_action` |

**完整查询示例**：

```sql
-- 1. 查询某 memory_id 对应的审计记录
SELECT audit_id, correlation_id, status, created_at
FROM governance.write_audit
WHERE evidence_refs_json->>'memory_id' = 'mem-abc123';

-- 2. 查询某 outbox_id 的完整审计链（同步阶段 + 异步阶段）
SELECT 
    audit_id, 
    correlation_id, 
    status, 
    reason,
    evidence_refs_json->>'source' AS source,
    created_at
FROM governance.write_audit
WHERE (evidence_refs_json->>'outbox_id')::int = 12345
ORDER BY created_at;

-- 3. 统计原意 allow 但被降级的记录数
SELECT COUNT(*) AS degraded_allow_count
FROM governance.write_audit
WHERE evidence_refs_json->>'intended_action' = 'allow'
  AND status = 'redirected';

-- 4. 联合查询：通过 outbox_id 关联同步审计与异步 flush 审计
SELECT 
    sync.audit_id AS sync_audit_id,
    sync.correlation_id AS sync_correlation_id,
    sync.status AS sync_status,
    async.audit_id AS async_audit_id,
    async.correlation_id AS async_correlation_id,
    async.reason AS async_reason
FROM governance.write_audit sync
JOIN governance.write_audit async 
  ON (sync.evidence_refs_json->>'outbox_id')::int = (async.evidence_refs_json->>'outbox_id')::int
WHERE sync.status = 'redirected'
  AND async.reason LIKE 'outbox_flush%';
```

#### 5.5.5 幂等性与并发安全

- **幂等性**：`jsonb ||` 合并操作是幂等的，重复调用会覆盖同名 key
- **并发安全**：`WHERE status = 'pending'` 条件确保只更新未 finalize 的记录
- **防重复 finalize**：如果记录已被其他进程 finalize（status 不再是 pending），`update_write_audit` 返回 `updated_count = 0`，调用方记录警告但不阻断流程

---

## 6. AUDIT_EVENT_SCHEMA_VERSION 与 Schema 对齐

### 6.1 当前版本

| 位置 | 版本号 | 说明 |
|------|--------|------|
| `src/engram/gateway/audit_event.py` | `AUDIT_EVENT_SCHEMA_VERSION = "2.0"` | 代码中的常量 |
| `schemas/audit_event_v2.schema.json` | `"schema_version": {"pattern": "^\\d+\\.\\d+$"}` | JSON Schema 格式约束 |

### 6.2 版本演进规则

```
版本约束规则：
- 主版本号变更（如 1.x → 2.x）：不兼容变更，需要迁移脚本
- 次版本号变更（如 1.0 → 1.1）：向后兼容，仅新增可选字段

演进原则：
- 新增字段必须有默认值或标记为可选（nullable）
- 禁止删除已有字段，仅可标记为 deprecated
- 读取时按 schema_version 做兼容处理
- 写入时始终使用 AUDIT_EVENT_SCHEMA_VERSION 常量
```

### 6.3 版本历史

| 版本 | 变更内容 |
|------|----------|
| 1.0 | 初始版本，包含 `source`/`operation`/`correlation_id`/`decision`/`evidence_summary` 等核心字段 |
| 1.1 | 新增 `gateway_event.policy` 和 `gateway_event.validation` 稳定子结构 |

### 6.4 Schema 与代码对齐要求

1. **修改 Schema 时**：必须同时更新 `AUDIT_EVENT_SCHEMA_VERSION`
2. **修改代码时**：确保生成的审计事件符合当前 Schema
3. **新增字段**：必须在 Schema 中标记为 `["string", "null"]` 或有默认值
4. **测试验证**：运行 `pytest tests/gateway/test_audit_event_contract.py -v`

### 6.5 Schema 文件引用

完整 Schema 定义见：`schemas/audit_event_v2.schema.json`

关键 definitions：

- `#/definitions/audit_event`：审计事件主结构
- `#/definitions/evidence_refs_json`：Logbook 兼容的完整结构
- `#/definitions/correlation_id`：correlation_id 格式（`^corr-[a-fA-F0-9]{16}$`）

---

## 7. 契约测试引用

> **详细测试追溯矩阵**：参见 [Gateway Audit/Outbox E2E 矩阵测试追溯](./gateway_audit_outbox_e2e_matrix_traceability.md)

### 7.1 correlation_id 相关测试

| 测试文件 | 测试类/方法 | 验证内容 |
|----------|-------------|----------|
| `tests/gateway/test_audit_event_contract.py` | `TestCorrelationIdFormat` | correlation_id 格式校验 |
| `tests/gateway/test_helpers_correlation_id_format.py` | * | correlation_id 生成与归一化 |
| `tests/gateway/test_error_codes.py` | `TestCorrelationIdInErrorResponses` | 错误响应中的 correlation_id 一致性 |

### 7.2 evidence_refs_json 相关测试

| 测试文件 | 测试类/方法 | 验证内容 |
|----------|-------------|----------|
| `tests/gateway/test_audit_event_contract.py` | `TestEvidenceRefsJsonLogbookQueryContract` | 顶层字段可查询性 |
| `tests/gateway/test_audit_event_contract.py` | `TestEvidenceRefsJsonSchema` | Schema 结构校验 |
| `tests/gateway/test_reconcile_outbox.py` | `TestAuditOutboxInvariants` | outbox/audit 闭环不变量 |

### 7.3 Outbox E2E 矩阵测试

| 测试文件 | 主要覆盖场景 | 覆盖点列表 |
|----------|--------------|------------|
| `tests/gateway/test_outbox_worker_integration.py` | Worker flush 成功/重试/dead/去重/冲突 | status, reason, action, evidence_refs_json 顶层字段 |
| `tests/gateway/test_reconcile_outbox.py` | Reconcile 补写审计/stale 检测 | status, reason, evidence_refs_json.outbox_id 可查询 |
| `tests/gateway/test_unified_stack_integration.py` | 降级与恢复全链路 | 完整审计链、correlation_id 传播 |

### 7.4 HTTP_ONLY_MODE 跳过说明

以下测试在 `HTTP_ONLY_MODE=1` 时跳过（不影响审计契约验证）：

| 场景 | 跳过原因 | 替代验证 |
|------|----------|----------|
| Outbox Worker 集成测试 | HTTP_ONLY 不支持 outbox 操作 | 单元测试覆盖 |
| Reconcile CLI 冒烟测试 | 需要 FULL profile | 单元测试覆盖 |
| 降级流程集成测试 | 依赖外部服务 | Mock 测试覆盖 |

### 7.5 快速验证命令

```bash
# correlation_id 相关测试
pytest tests/gateway/test_helpers_correlation_id_format.py -v

# 审计契约完整检查
pytest tests/gateway/test_audit_event_contract.py -v

# 闭环测试
pytest tests/gateway/test_reconcile_outbox.py -v

# Outbox E2E 全场景验证
pytest tests/gateway/test_outbox_worker_integration.py \
       tests/gateway/test_reconcile_outbox.py \
       tests/gateway/test_unified_stack_integration.py -v
```

---

## 8. 必须通过的命令集合

修改审计/证据/关联性相关代码后，**必须**运行以下检查：

### 8.1 核心契约验证（必须全部通过）

```bash
# 1. DI 边界检查
python scripts/ci/check_gateway_di_boundaries.py --verbose

# 2. 审计事件契约测试
pytest tests/gateway/test_audit_event_contract.py -v

# 3. correlation_id 格式测试
pytest tests/gateway/test_helpers_correlation_id_format.py -v

# 4. Outbox 闭环测试
pytest tests/gateway/test_reconcile_outbox.py -v

# 5. 错误响应 correlation_id 一致性
pytest tests/gateway/test_error_codes.py -v

# 6. MCP JSON-RPC 契约
pytest tests/gateway/test_mcp_jsonrpc_contract.py -v
```

### 8.2 完整验收命令（一键运行）

```bash
# 一键运行所有审计/证据/关联性契约测试
python scripts/ci/check_gateway_di_boundaries.py --verbose && \
pytest tests/gateway/test_audit_event_contract.py \
       tests/gateway/test_helpers_correlation_id_format.py \
       tests/gateway/test_reconcile_outbox.py \
       tests/gateway/test_error_codes.py \
       tests/gateway/test_mcp_jsonrpc_contract.py \
       tests/gateway/test_unified_stack_integration.py -v
```

### 8.3 CI 门禁映射

| 检查项 | CI Job | 失败处理 |
|--------|--------|----------|
| DI 边界检查 | `gateway-di-boundaries` | 阻止合并 |
| 审计契约测试 | `test (gateway/)` | 阻止合并 |
| correlation_id 格式 | `test (gateway/)` | 阻止合并 |
| Outbox 闭环测试 | `test (gateway/)` | 阻止合并 |
| 端到端集成测试 | `test (acceptance/)` | 阻止合并 |

---

## 9. 相关文档

| 文档 | 说明 |
|------|------|
| [Gateway 设计](../gateway/06_gateway_design.md) | correlation_id 单一来源原则、启动链路 |
| [ADR: Gateway DI 与入口边界统一](../architecture/adr_gateway_di_and_entry_boundary.md) | 依赖注入与 correlation_id 生成位置设计决策 |
| [Gateway ImportError 与可选依赖处理规范](../architecture/gateway_importerror_and_optional_deps.md) | 模块导入时机与可选性 |
| [Gateway ↔ Logbook 边界契约](./gateway_logbook_boundary.md) | Logbook 原语接口定义 |
| [Outbox Lease 契约](./outbox_lease_v2.md) | Outbox Worker 租约协议 |
| [**Audit/Outbox E2E 矩阵测试追溯**](./gateway_audit_outbox_e2e_matrix_traceability.md) | 场景矩阵、主测试映射、覆盖点、缺口识别 |
| [audit_event_v2.schema.json](../../schemas/audit_event_v2.schema.json) | 审计事件 JSON Schema |

---

## 10. Strict / Compat 模式 Evidence 规范

### 9.1 模式概述

Gateway 支持两种 evidence 处理模式，控制校验严格程度和向后兼容性：

| 模式 | 输入格式 | sha256 要求 | validate_refs | 适用场景 |
|------|----------|-------------|---------------|----------|
| **strict** | evidence (v2) | 必填，64位十六进制 | 强制启用 | 新接入系统，需要完整可回跳 |
| **compat** | evidence (v2) 或 evidence_refs (v1) | 可选 | 可配置 | 旧系统迁移，向后兼容 |

### 9.2 输入字段说明

#### 9.2.1 evidence (v2 格式) - 推荐

```json
{
    "evidence": [
        {
            "uri": "memory://attachments/123/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "kind": "screenshot"
        },
        {
            "uri": "memory://patch_blobs/git/1:abc123/a1b2c3d4e5f67890...",
            "sha256": "a1b2c3d4e5f67890...",
            "source_type": "git",
            "source_id": "1:abc123"
        }
    ]
}
```

#### 9.2.2 evidence_refs (v1 格式) - 仅 compat 模式

```json
{
    "evidence_refs": [
        "https://example.com/doc.md",
        "git://repo/commit/abc123",
        "svn://repo/trunk@100"
    ]
}
```

**映射规则**: v1 evidence_refs 在 compat 模式下自动映射为 v2 external 格式：

```json
{
    "external": [
        {
            "uri": "https://example.com/doc.md",
            "sha256": "",
            "_source": "evidence_refs_legacy"
        }
    ]
}
```

### 9.3 校验规则

#### 9.3.1 strict 模式校验规则

| 字段 | 要求 | 失败时 error_code |
|------|------|-------------------|
| uri | 必填 | `EVIDENCE_MISSING_URI` |
| sha256 | 必填，64位十六进制 | `EVIDENCE_MISSING_SHA256` 或 `EVIDENCE_INVALID_SHA256` |

**strict 模式行为**：

1. 校验失败时 **阻断请求**，返回 `action=reject`
2. 审计记录 `gateway_event.validation.evidence_validation.error_codes` 包含具体错误码
3. 响应 message 包含失败详情

#### 9.3.2 compat 模式校验规则

| 字段 | 要求 | 失败时行为 |
|------|------|------------|
| uri | 必填（v2）或自动从 evidence_refs 提取 | 记录警告 |
| sha256 | 可选（legacy 来源允许为空） | 记录 `compat_warnings` |

**compat 模式行为**：

1. legacy evidence_refs 映射为 external 后 **不触发阻断**
2. 缺少 sha256 的 legacy 来源记录 `EVIDENCE_LEGACY_NO_SHA256` 警告
3. 警告信息写入 `gateway_event.validation.evidence_validation.compat_warnings`

### 9.4 审计字段说明

#### 9.4.1 gateway_event.validation 子结构

strict/compat 模式下，审计记录的 `gateway_event.validation` 包含校验上下文：

```json
{
    "gateway_event": {
        "validation": {
            "validate_refs_effective": true,
            "validate_refs_reason": "strict_enforced",
            "evidence_validation": {
                "is_valid": false,
                "error_codes": ["EVIDENCE_MISSING_SHA256:evidence[0]:memory://..."],
                "compat_warnings": []
            }
        }
    }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `validate_refs_effective` | bool | 实际生效的 validate_refs 值 |
| `validate_refs_reason` | string | 决策原因（strict_enforced / strict_env_override / compat_default 等） |
| `evidence_validation` | object/null | 校验详情（strict 模式下存在） |
| `evidence_validation.is_valid` | bool | 校验是否通过 |
| `evidence_validation.error_codes` | array | 错误码列表 |
| `evidence_validation.compat_warnings` | array | 兼容性警告列表 |

#### 9.4.2 gateway_event.policy 子结构

策略决策上下文记录在 `gateway_event.policy`：

```json
{
    "gateway_event": {
        "policy": {
            "mode": "strict",
            "mode_reason": "from_settings",
            "policy_version": "v1",
            "is_pointerized": false,
            "policy_source": "settings"
        }
    }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `mode` | string | 策略模式 "strict" / "compat" |
| `mode_reason` | string | 模式判定说明 |
| `policy_version` | string | 策略版本 "v1" / "v2" |
| `is_pointerized` | bool | 是否 pointerized（v2 特性） |
| `policy_source` | string | 策略来源 "settings" / "default" / "override" |

### 9.5 error_codes 完整列表

| 错误码 | 触发条件 | 模式 | 行为 |
|--------|----------|------|------|
| `EVIDENCE_MISSING_URI` | evidence 项缺少 uri 字段 | strict | 阻断 |
| `EVIDENCE_MISSING_SHA256` | v2 evidence 项缺少 sha256 字段 | strict | 阻断 |
| `EVIDENCE_INVALID_SHA256` | sha256 格式无效（非 64 位十六进制） | strict | 阻断 |
| `EVIDENCE_LEGACY_NO_SHA256` | legacy 来源的 evidence 无 sha256 | compat | 警告（不阻断） |

### 9.6 配置开关

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `VALIDATE_EVIDENCE_REFS` | false | 是否启用 evidence refs 结构校验 |
| `STRICT_MODE_ENFORCE_VALIDATE_REFS` | true | strict 模式下是否强制启用校验 |

**决策逻辑**：

```
if mode == "strict":
    if STRICT_MODE_ENFORCE_VALIDATE_REFS:
        validate_refs = True  (强制，忽略所有 override)
    else:
        validate_refs = VALIDATE_EVIDENCE_REFS 或 caller_override
else:  # compat
    validate_refs = VALIDATE_EVIDENCE_REFS 或 caller_override
```

---

## 11. 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-02-01 | v1.0 | 初始版本 |
| 2026-02-01 | v1.1 | 新增 strict/compat 模式 evidence 规范、校验规则、审计字段说明 |
| 2026-02-01 | v1.2 | 新增第 8 章「必须通过的命令集合」，关联 DI 边界检查与核心契约测试 |
| 2026-02-01 | v1.3 | 新增 5.4 节 evidence_refs_json_patch 写入时机，支持通过 finalize_audit 在 finalize 阶段合并 memory_id/outbox_id/intended_action 到顶层，不再依赖 reason 字段做跨阶段查询 |
| 2026-02-01 | v1.4 | 5.4 节明确声明选择方案 A（顶层字段后置合并），补充 jsonb 合并幂等性说明和底层实现代码引用 |
| 2026-02-01 | v1.5 | 新增 5.5 节「finalize 写回字段规范」，详细定义 memory_id/outbox_id/intended_action 的数据类型、写入时机、SQL 查询依赖及完整示例 |
