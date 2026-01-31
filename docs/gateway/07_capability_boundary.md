# Gateway 能力边界与契约

本文档定义 Memory Gateway 对外暴露的 HTTP 端点、MCP 协议边界、工具契约、依赖关系与不变量约束。

> **术语说明**：Memory Gateway 是 Gateway 组件的完整名称，后续简称 Gateway。详见 [命名规范](../architecture/naming.md)。

---

## 目录

- [对外端点契约](#对外端点契约)
- [MCP 协议边界](#mcp-协议边界)
- [工具契约（AVAILABLE_TOOLS）](#工具契约available_tools)
- [依赖关系](#依赖关系)
- [关键不变量](#关键不变量)
- [验收用例索引](#验收用例索引)
- [Reconcile 对账能力](#reconcile-对账能力)

---

## 对外端点契约

Gateway 对外暴露以下 HTTP 端点：

### `/health` - 健康检查

| 属性 | 值 |
|------|-----|
| **方法** | `GET` |
| **鉴权** | 无 |
| **用途** | 检查服务健康状态 |

**请求**：无参数

**响应**：

```json
{
  "ok": true,
  "status": "ok",
  "service": "memory-gateway"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | 服务是否健康 |
| `status` | string | 状态字符串（向后兼容） |
| `service` | string | 服务名称 |

**测试引用**：[`test_unified_stack_integration.py::TestServiceHealthCheck`](../../tests/gateway/test_unified_stack_integration.py)

---

### `/mcp` - MCP 统一入口

| 属性 | 值 |
|------|-----|
| **方法** | `POST`, `OPTIONS` |
| **鉴权** | 工具级别（部分工具需要 admin_key 或 allowlist 鉴权） |
| **用途** | MCP 工具调用统一入口，支持双协议 |

**请求头**：

| Header | 必需 | 说明 |
|--------|------|------|
| `Content-Type` | 是 | `application/json` |
| `Mcp-Session-Id` | 否 | MCP 会话 ID（用于日志关联） |

**请求格式**：自动识别两种格式

1. **JSON-RPC 2.0 格式**（推荐）：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "memory_store",
    "arguments": {
      "payload_md": "..."
    }
  }
}
```

2. **旧格式**（兼容）：

```json
{
  "tool": "memory_store",
  "arguments": {
    "payload_md": "..."
  }
}
```

**响应**：见 [MCP 协议边界](#mcp-协议边界) 章节

**CORS 支持**：

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: POST, OPTIONS
Access-Control-Allow-Headers: Content-Type, Authorization, Mcp-Session-Id
```

**测试引用**：
- [`test_mcp_jsonrpc_contract.py`](../../tests/gateway/test_mcp_jsonrpc_contract.py) - JSON-RPC 协议契约
- [`test_unified_stack_integration.py::TestJsonRpcProtocol`](../../tests/gateway/test_unified_stack_integration.py) - JSON-RPC 集成
- [`test_unified_stack_integration.py::TestLegacyProtocol`](../../tests/gateway/test_unified_stack_integration.py) - 旧协议兼容

---

### `/memory/store` - 存储记忆（REST）

| 属性 | 值 |
|------|-----|
| **方法** | `POST` |
| **鉴权** | 无（策略校验在内部进行） |
| **用途** | REST 风格的记忆存储接口 |

**请求 Schema**：`MemoryStoreRequest`

```json
{
  "payload_md": "string (required)",
  "target_space": "string (optional, default: team:<project>)",
  "meta_json": "object (optional)",
  "kind": "string (optional: FACT/PROCEDURE/PITFALL/DECISION/REVIEW_GUIDE)",
  "evidence_refs": ["string"] "(optional, v1 legacy format)",
  "evidence": [{"type": "...", "uri": "...", "sha256": "..."}] "(optional, v2 format)",
  "is_bulk": "boolean (optional, default: false)",
  "item_id": "integer (optional)",
  "actor_user_id": "string (optional)"
}
```

**响应 Schema**：`MemoryStoreResponse`

```json
{
  "ok": true,
  "action": "allow",  // allow / redirect / reject / error
  "space_written": "team:default",
  "memory_id": "mem_xxx",
  "evidence_refs": ["..."],
  "message": null
}
```

| action 值 | ok 值 | 说明 |
|-----------|-------|------|
| `allow` | `true` | 写入成功 |
| `redirect` | `true/false` | 降级写入（策略导向或失败降级） |
| `reject` | `false` | 策略拒绝 |
| `error` | `false` | 内部错误或审计失败 |

**测试引用**：[`test_unified_stack_integration.py::TestMemoryOperations`](../../tests/gateway/test_unified_stack_integration.py)

---

### `/memory/query` - 查询记忆（REST）

| 属性 | 值 |
|------|-----|
| **方法** | `POST` |
| **鉴权** | 无 |
| **用途** | REST 风格的记忆查询接口 |

**请求 Schema**：`MemoryQueryRequest`

```json
{
  "query": "string (required)",
  "spaces": ["string"] "(optional)",
  "filters": "object (optional)",
  "top_k": "integer (optional, default: 10)"
}
```

**响应 Schema**：`MemoryQueryResponse`

```json
{
  "ok": true,
  "results": [
    {
      "id": "...",
      "content": "...",
      "score": 0.95
    }
  ],
  "total": 5,
  "spaces_searched": ["team:default"],
  "message": null,
  "degraded": false
}
```

| 字段 | 说明 |
|------|------|
| `degraded` | `true` 表示结果来自 Logbook 回退查询（OpenMemory 不可用时） |

**降级行为**：当 OpenMemory 查询失败时，自动降级到 Logbook 的 `knowledge_candidates` 表进行回退查询。

**测试引用**：[`test_unified_stack_integration.py::TestMemoryOperations`](../../tests/gateway/test_unified_stack_integration.py)

---

### `/reliability/report` - 可靠性报告

| 属性 | 值 |
|------|-----|
| **方法** | `GET` |
| **鉴权** | 无（只读） |
| **用途** | 获取 outbox 和 audit 的统计数据 |

**请求**：无参数

**响应 Schema**：`ReliabilityReportResponse`

```json
{
  "ok": true,
  "outbox_stats": {
    "pending": 0,
    "sent": 10,
    "dead": 0,
    "total": 10
  },
  "audit_stats": {
    "allow": 100,
    "redirect": 5,
    "reject": 2,
    "total": 107
  },
  "v2_evidence_stats": {
    "total_audits_with_v2": 50,
    "coverage_percent": 46.73
  },
  "content_intercept_stats": {
    "total": 0
  },
  "generated_at": "2026-01-30T10:00:00Z",
  "message": null
}
```

**Schema 定义**：[`schemas/reliability_report_v1.schema.json`](../../schemas/reliability_report_v1.schema.json)

**测试引用**：
- [`test_reliability_report_contract.py`](../../tests/gateway/test_reliability_report_contract.py) - 报告结构契约
- [`test_unified_stack_integration.py::TestReliabilityReport`](../../tests/gateway/test_unified_stack_integration.py) - 集成测试

---

### `/governance/settings/update` - 治理设置更新

| 属性 | 值 |
|------|-----|
| **方法** | `POST` |
| **鉴权** | admin_key 或 allowlist_users |
| **用途** | 更新项目的治理设置 |

**请求 Schema**：`GovernanceSettingsUpdateRequest`

```json
{
  "team_write_enabled": true,
  "policy_json": {"key": "value"},
  "admin_key": "secret",
  "actor_user_id": "user_001"
}
```

**鉴权方式**（满足其一）：

1. `admin_key` 与环境变量 `GOVERNANCE_ADMIN_KEY` 匹配
2. `actor_user_id` 在当前 `policy_json.allowlist_users` 中

**响应 Schema**：`GovernanceSettingsUpdateResponse`

```json
{
  "ok": true,
  "action": "allow",  // allow / reject
  "settings": {
    "team_write_enabled": true,
    "policy_json": {...}
  },
  "message": null
}
```

**审计记录**：所有更新尝试（无论成功失败）都会写入 `governance.write_audit` 表。

**测试引用**：[`test_unified_stack_integration.py`](../../tests/gateway/test_unified_stack_integration.py)

---

## MCP 协议边界

Gateway 的 `/mcp` 端点支持两种协议格式，实现双协议兼容。

### JSON-RPC 2.0 协议（推荐）

**支持的方法**：

| 方法 | 用途 | 参数 |
|------|------|------|
| `tools/list` | 返回可用工具清单 | 无 |
| `tools/call` | 调用指定工具 | `{name, arguments}` |

#### `tools/list` 响应

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "memory_store",
        "description": "存储记忆到 OpenMemory...",
        "inputSchema": {
          "type": "object",
          "properties": {...},
          "required": ["payload_md"]
        }
      }
      // ...更多工具
    ]
  }
}
```

#### `tools/call` 成功响应

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"ok\":true,\"action\":\"allow\",...}"
      }
    ]
  }
}
```

#### 错误响应结构（ErrorData）

所有 JSON-RPC 错误都返回结构化的 `error.data`：

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
      "correlation_id": "corr-abc123",
      "details": {"service": "openmemory"}
    }
  }
}
```

**错误分类（category）**：

| 分类 | 说明 | 可重试 |
|------|------|--------|
| `protocol` | JSON-RPC 格式错误、方法不存在 | 否 |
| `validation` | 参数校验错误 | 否 |
| `business` | 业务拒绝（策略、鉴权） | 否 |
| `dependency` | 依赖服务不可用 | 通常是 |
| `internal` | 内部错误 | 否 |

**错误码（code）**：

| 错误码 | 名称 | 说明 |
|--------|------|------|
| `-32700` | PARSE_ERROR | JSON 解析失败 |
| `-32600` | INVALID_REQUEST | 无效请求 |
| `-32601` | METHOD_NOT_FOUND | 方法不存在 |
| `-32602` | INVALID_PARAMS | 无效参数 |
| `-32603` | INTERNAL_ERROR | 内部错误 |
| `-32000` | TOOL_EXECUTION_ERROR | 工具执行错误 |
| `-32001` | DEPENDENCY_UNAVAILABLE | 依赖服务不可用 |
| `-32002` | BUSINESS_REJECTION | 业务拒绝 |

**代码定义**：[`mcp_rpc.py::JsonRpcErrorCode`](../../src/engram/gateway/mcp_rpc.py)

---

### 旧协议格式（兼容）

**请求**：

```json
{
  "tool": "memory_store",
  "arguments": {
    "payload_md": "..."
  }
}
```

**成功响应**：

```json
{
  "ok": true,
  "result": {
    "ok": true,
    "action": "allow",
    "space_written": "team:default",
    "memory_id": "mem_xxx"
  }
}
```

**错误响应**：

```json
{
  "ok": false,
  "error": "错误消息"
}
```

---

### 协议兼容策略

| 场景 | 行为 |
|------|------|
| 请求含 `jsonrpc: "2.0"` 字段 | 走 JSON-RPC 2.0 分支 |
| 请求含 `tool` 字段但无 `jsonrpc` | 走旧协议分支 |
| 两种字段都有 | 优先 JSON-RPC 2.0 |

**迁移建议**：新客户端应使用 JSON-RPC 2.0 格式，旧格式仅为向后兼容保留。

---

## 工具契约（AVAILABLE_TOOLS）

Gateway 通过 MCP 暴露以下工具，定义在 [`mcp_rpc.py::AVAILABLE_TOOLS`](../../src/engram/gateway/mcp_rpc.py)。

### `memory_store` - 存储记忆

| 属性 | 值 |
|------|-----|
| **描述** | 存储记忆到 OpenMemory，含策略校验、审计、失败降级到 outbox |
| **必需参数** | `payload_md` |

**输入参数**：

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `payload_md` | string | **是** | 记忆内容（Markdown 格式） |
| `target_space` | string | 否 | 目标空间，默认 `team:<project>` |
| `meta_json` | object | 否 | 元数据 |
| `kind` | string | 否 | 知识类型：FACT/PROCEDURE/PITFALL/DECISION/REVIEW_GUIDE |
| `evidence_refs` | array[string] | 否 | 证据链引用（v1 格式） |
| `evidence` | array[object] | 否 | 结构化证据（v2 格式） |
| `is_bulk` | boolean | 否 | 是否为批量提交（默认 false） |
| `item_id` | integer | 否 | 关联的 logbook.items.item_id |
| `actor_user_id` | string | 否 | 执行操作的用户标识 |

**返回结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | 操作是否成功 |
| `action` | string | 动作类型：allow/redirect/reject/error |
| `space_written` | string | 实际写入的空间 |
| `memory_id` | string | OpenMemory 返回的 memory ID |
| `evidence_refs` | array | 证据引用 |
| `message` | string | 附加消息 |

**失败/降级语义**：

| action | ok | 含义 |
|--------|-----|------|
| `allow` | true | 成功写入 OpenMemory |
| `redirect` | true/false | 策略降级或失败入 outbox |
| `reject` | false | 策略拒绝，未写入 |
| `error` | false | 内部错误（如审计写入失败） |

---

### `memory_query` - 查询记忆

| 属性 | 值 |
|------|-----|
| **描述** | 查询记忆，支持多空间搜索和过滤 |
| **必需参数** | `query` |

**输入参数**：

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `query` | string | **是** | 查询文本 |
| `spaces` | array[string] | 否 | 搜索空间列表 |
| `filters` | object | 否 | 过滤条件 |
| `top_k` | integer | 否 | 返回结果数量（默认 10） |

**返回结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | 查询是否成功 |
| `results` | array | 查询结果列表 |
| `total` | integer | 结果总数 |
| `spaces_searched` | array | 搜索的空间 |
| `message` | string | 附加消息 |
| `degraded` | boolean | 是否为降级查询结果 |

**降级语义**：当 `degraded=true` 时，结果来自 Logbook 的 `knowledge_candidates` 表回退查询。

---

### `reliability_report` - 可靠性报告

| 属性 | 值 |
|------|-----|
| **描述** | 获取可靠性统计报告（只读） |
| **必需参数** | 无 |

**输入参数**：无

**返回结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | 获取是否成功 |
| `outbox_stats` | object | outbox_memory 表统计 |
| `audit_stats` | object | write_audit 表统计 |
| `v2_evidence_stats` | object | v2 evidence 覆盖率统计 |
| `content_intercept_stats` | object | 内容拦截统计 |
| `generated_at` | string | 报告生成时间（ISO 8601） |

---

### `governance_update` - 更新治理设置

| 属性 | 值 |
|------|-----|
| **描述** | 更新治理设置（需鉴权） |
| **必需参数** | 无（但需要 admin_key 或 actor_user_id 用于鉴权） |

**输入参数**：

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `team_write_enabled` | boolean | 否 | 是否启用团队写入 |
| `policy_json` | object | 否 | 策略 JSON |
| `admin_key` | string | 否 | 管理密钥（与 GOVERNANCE_ADMIN_KEY 匹配） |
| `actor_user_id` | string | 否 | 用户标识（用于 allowlist 鉴权） |

**返回结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | 更新是否成功 |
| `action` | string | 动作类型：allow/reject |
| `settings` | object | 更新后的设置 |
| `message` | string | 附加消息 |

**鉴权失败语义**：`ok=false, action="reject"`，并返回具体的拒绝原因。

---

### `evidence_upload` - 上传证据

| 属性 | 值 |
|------|-----|
| **描述** | 上传证据文件到存储后端 |
| **必需参数** | `content`, `content_type`（工具内校验） |

**输入参数**：

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `content` | string | **是** | 证据内容（base64 或文本） |
| `content_type` | string | **是** | MIME 类型（如 text/plain, application/json） |
| `title` | string | 否 | 证据标题/文件名 |
| `actor_user_id` | string | 否 | 执行操作的用户标识 |
| `project_key` | string | 否 | 项目标识 |
| `item_id` | integer | 否 | 关联的 item_id（缺失时自动创建） |

**返回结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | 上传是否成功 |
| `item_id` | integer | 关联的 item ID |
| `attachment_id` | integer | 附件 ID |
| `sha256` | string | 内容哈希 |
| `evidence` | object | v2 evidence 对象 |
| `artifact_uri` | string | 存储 URI |
| `size_bytes` | integer | 内容大小 |
| `content_type` | string | MIME 类型 |

**失败语义**：

| error_code | 含义 | retryable |
|------------|------|-----------|
| `EVIDENCE_SIZE_LIMIT_EXCEEDED` | 超过大小限制 | false |
| `EVIDENCE_CONTENT_TYPE_NOT_ALLOWED` | 不允许的 MIME 类型 | false |
| `EVIDENCE_WRITE_ERROR` | 存储写入失败 | true |
| `MISSING_REQUIRED_PARAMETER` | 缺少必需参数 | false |

---

## 依赖关系

### Logbook 原语依赖

Gateway 依赖 `engram_logbook` 提供的原语接口：

| 模块 | 依赖函数 | 用途 |
|------|----------|------|
| **governance** | `get_or_create_settings()` | 获取治理设置 |
| | `upsert_settings()` | 更新治理设置 |
| | `insert_write_audit()` | 写入审计记录 |
| | `query_write_audit()` | 查询审计记录 |
| **outbox** | `enqueue_memory()` | 入队补偿队列 |
| | `check_dedup()` | 去重检查 |
| | `claim_outbox()` | 获取待处理记录 |
| | `ack_sent()` | 确认发送成功 |
| | `fail_retry()` | 标记失败重试 |
| | `mark_dead_by_worker()` | 标记死信 |
| **uri** | `build_evidence_uri()` | 构建证据 URI |
| | `parse_evidence_uri()` | 解析证据 URI |

→ 完整接口签名：[docs/contracts/gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md#附录接口签名速查)

### OpenMemory 依赖

| 操作 | 依赖 | 降级行为 |
|------|------|----------|
| **写入** | `OpenMemoryClient.store()` | 失败入 outbox，后台重试 |
| **查询** | `OpenMemoryClient.search()` | 降级到 Logbook `knowledge_candidates` |

### 运行模式差异（HTTP_ONLY / FULL）

| 能力 | HTTP_ONLY | FULL |
|------|-----------|------|
| 健康检查 (`/health`) | ✓ | ✓ |
| MCP 工具调用 (`/mcp`) | ✓ | ✓ |
| memory_store/query | ✓ | ✓ |
| reliability_report | ✓ | ✓ |
| governance_update | ✓ | ✓ |
| **真实降级流程测试** | 跳过 | ✓ |
| **Outbox Worker 集成** | 跳过 | ✓ |
| **Docker 容器操作** | 跳过 | ✓ |

**环境变量控制**：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HTTP_ONLY_MODE` | `0` | 设为 `1` 跳过需要 Docker 操作的测试 |
| `VERIFY_FULL` | `0` | 设为 `1` 启用完整验证（含降级测试） |

→ 详见 [docs/acceptance/00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md#gateway--logbook-覆盖点)

---

## 关键不变量

以下不变量必须在系统运行过程中始终成立，违反时应触发告警。

### 1. Audit ↔ Outbox 对应关系

```sql
-- redirect action 必须有对应的 outbox 记录
audit.count(action='redirect' AND reason LIKE 'OPENMEMORY_%') 
  == outbox.count(status IN ('pending', 'sent', 'dead'))
```

**验证方式**：`reconcile_outbox` 定期检查

**测试引用**：[`test_reconcile_outbox.py::TestAuditOutboxInvariants`](../../tests/gateway/test_reconcile_outbox.py)

**不变量映射表**：

| outbox 状态 | 审计 reason | 审计 action |
|-------------|-------------|-------------|
| `sent` | `outbox_flush_success` / `outbox_flush_dedup_hit` | allow |
| `dead` | `outbox_flush_dead` | reject |
| `pending` (stale) | `outbox_stale` | redirect |

### 2. Reliability Report 统计口径

```sql
-- 报告必须基于完整的 audit 数据
reliability_report.audit_stats.total == COUNT(*) FROM governance.write_audit

-- 成功率计算
success_rate = audit_stats.allow / audit_stats.total
```

**验证方式**：报告生成时聚合校验

**测试引用**：[`test_reliability_report_contract.py`](../../tests/gateway/test_reliability_report_contract.py)

### 3. Lease 冲突处理的审计要求

当 outbox 记录的 lease 过期被其他 worker 抢占时：

| 场景 | 审计要求 |
|------|----------|
| Lease 过期重新调度 | 必须写入 `outbox_stale` 审计事件 |
| Worker 处理中 lease 续期失败 | 记录 warning 但不写审计 |
| 死信处理 | 必须写入 `outbox_flush_dead` 审计事件 |

**代码实现**：[`src/engram/gateway/reconcile_outbox.py`](../../src/engram/gateway/reconcile_outbox.py)

**测试引用**：[`test_outbox_worker.py`](../../tests/gateway/test_outbox_worker.py)

### 4. Evidence refs_json 可查询性

```sql
-- outbox_id 必须在 evidence_refs_json 顶层，确保可查询
SELECT * FROM governance.write_audit 
WHERE evidence_refs_json->>'outbox_id' = ?
```

**测试引用**：[`test_audit_event_contract.py::TestEvidenceRefsJsonLogbookQueryContract`](../../tests/gateway/test_audit_event_contract.py)

---

## 验收用例索引

本节将能力边界映射到现有测试文件。

### 端点契约测试

| 端点 | 测试文件 | 测试类/函数 |
|------|----------|-------------|
| `/health` | `test_unified_stack_integration.py` | `TestServiceHealthCheck` |
| `/mcp` (JSON-RPC) | `test_mcp_jsonrpc_contract.py` | 全部 |
| `/mcp` (集成) | `test_unified_stack_integration.py` | `TestJsonRpcProtocol`, `TestLegacyProtocol` |
| `/memory/store` | `test_unified_stack_integration.py` | `TestMemoryOperations`, `TestMCPMemoryStoreE2E` |
| `/memory/query` | `test_unified_stack_integration.py` | `TestMemoryOperations` |
| `/reliability/report` | `test_reliability_report_contract.py` | 全部 |
| `/governance/settings/update` | `test_unified_stack_integration.py` | （隐式覆盖） |

### 协议契约测试

| 协议 | 测试文件 | 覆盖内容 |
|------|----------|----------|
| JSON-RPC 2.0 | `test_mcp_jsonrpc_contract.py` | 请求解析、错误码、ErrorData 结构 |
| 旧协议兼容 | `test_unified_stack_integration.py::TestLegacyProtocol` | tool/arguments 格式 |
| 双协议切换 | `test_mcp_jsonrpc_contract.py` | 自动识别逻辑 |

### 不变量测试

| 不变量 | 测试文件 | 测试类/函数 |
|--------|----------|-------------|
| audit↔outbox 闭环 | `test_reconcile_outbox.py` | `TestAuditOutboxInvariants` |
| evidence_refs_json 查询 | `test_audit_event_contract.py` | `TestEvidenceRefsJsonLogbookQueryContract` |
| reliability_report 结构 | `test_reliability_report_contract.py` | 全部 |

### 降级测试

| 场景 | 测试文件 | 前置条件 |
|------|----------|----------|
| Mock 降级流程 | `test_unified_stack_integration.py::TestMockDegradationFlow` | `POSTGRES_DSN` |
| Mock 查询降级 | `test_unified_stack_integration.py::TestMockQueryDegradation` | `POSTGRES_DSN` |
| 真实降级流程 | `test_unified_stack_integration.py::TestDegradationFlow` | Docker 权限（`HTTP_ONLY_MODE=0`） |
| Outbox Worker 集成 | `test_outbox_worker_integration.py` | Docker 权限 |

### 运行方式

```bash
# HTTP_ONLY 模式（CI 推荐）
make test-gateway-integration

# FULL 模式（含降级测试）
make test-gateway-integration-full

# 单独运行契约测试
cd "$PROJECT_ROOT"
pytest tests/gateway/test_mcp_jsonrpc_contract.py -v
pytest tests/gateway/test_reliability_report_contract.py -v
pytest tests/gateway/test_audit_event_contract.py -v
```

---

## Reconcile 对账能力

`reconcile_outbox` 模块提供 Outbox 与 Audit 数据一致性对账能力，确保系统在异常情况下的数据完整性。

### 能力边界

| 属性 | 说明 |
|------|------|
| **功能** | 检测并修复 outbox_memory 与 write_audit 的数据不一致 |
| **触发方式** | 命令行工具 `python -m gateway.reconcile_outbox` |
| **调度方式** | 可作为 cron job 定期执行 |

### 输入参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--scan-window` | int | 24 | 扫描时间窗口（小时），扫描最近 N 小时内更新的记录 |
| `--batch-size` | int | 100 | 批量处理大小，每轮最多处理 N 条记录 |
| `--stale-threshold` | int | 600 | Stale 阈值（秒），locked_at 超过此时间视为 stale（默认 10 分钟） |
| `--no-auto-fix` | flag | false | 仅检测不修复（report 模式） |
| `--no-reschedule` | flag | false | 不重新调度 stale 记录 |
| `--reschedule-delay` | int | 0 | 重新调度延迟（秒） |

### 输出格式

#### 摘要报告（stdout）

```
=== Outbox Reconcile Report ===
Total scanned: 150
  - sent:  80 (missing audit: 2, fixed: 2)
  - dead:  10 (missing audit: 1, fixed: 1)
  - stale: 5 (missing audit: 3, fixed: 3, rescheduled: 3)
```

| 字段 | 说明 |
|------|------|
| `Total scanned` | 本轮扫描的 outbox 记录总数 |
| `sent` | status=sent 的记录数及缺失/修复统计 |
| `dead` | status=dead 的记录数及缺失/修复统计 |
| `stale` | pending 且 locked 过期的记录数及修复/重调度统计 |

#### 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 成功：所有检测到的缺失审计都已修复 |
| `1` | 部分失败：存在未修复的缺失审计（如 auto_fix=false） |
| `2` | 执行错误：程序异常终止 |

### 不改写业务结果（边界约束）

Reconcile 对账模块遵循以下边界约束：

1. **只读业务数据**：不修改 `outbox_memory` 的 `payload_md`、`payload_sha` 等业务字段
2. **只补写审计**：仅在 `governance.write_audit` 中补写缺失的审计记录
3. **只更新调度字段**：对 stale 记录仅更新 `next_attempt_at`、`locked_at`、`locked_by` 调度相关字段
4. **幂等性**：重复执行不会产生重复审计（通过 outbox_id 去重检测）

### 对账逻辑映射

| outbox 状态 | 期望审计 reason | 审计 action | 补写条件 |
|-------------|-----------------|-------------|----------|
| `sent` | `outbox_flush_success` 或 `outbox_flush_dedup_hit` | `allow` | 缺失时补写 |
| `dead` | `outbox_flush_dead` | `reject` | 缺失时补写 |
| `pending` (stale) | `outbox_stale` | `redirect` | locked 超时且缺失时补写 |

### 运行方式

```bash
# 执行一轮对账（修复模式）
python -m gateway.reconcile_outbox --once

# 仅报告不修复
python -m gateway.reconcile_outbox --report

# 自定义参数
python -m gateway.reconcile_outbox --once \
  --scan-window 48 \
  --batch-size 200 \
  --stale-threshold 1200

# 详细日志
python -m gateway.reconcile_outbox --once -v
```

### 测试引用

| 测试类型 | 测试文件 | 覆盖内容 |
|----------|----------|----------|
| 单元测试 | `test_reconcile_outbox.py::TestReconcileSentRecords` | sent 状态对账 |
| 单元测试 | `test_reconcile_outbox.py::TestReconcileDeadRecords` | dead 状态对账 |
| 单元测试 | `test_reconcile_outbox.py::TestReconcileStaleRecords` | stale 状态对账 |
| 契约测试 | `test_reconcile_outbox.py::TestReconcileReasonErrorCodeContract` | ErrorCode 一致性 |
| 不变量测试 | `test_reconcile_outbox.py::TestAuditOutboxInvariants` | 审计/Outbox 闭环 |
| 冒烟测试 | `test_reconcile_outbox.py::TestReconcileSmokeTest` | 命令行退出码与摘要格式 |

---

## 相关文档

| 主题 | 文档路径 |
|------|----------|
| Gateway 设计 | [docs/gateway/06_gateway_design.md](./06_gateway_design.md) |
| Gateway ↔ Logbook 边界 | [docs/contracts/gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md) |
| 失败降级流程 | [docs/gateway/05_failure_degradation.md](./05_failure_degradation.md) |
| 治理开关 | [docs/gateway/04_governance_switch.md](./04_governance_switch.md) |
| 验收测试矩阵 | [docs/acceptance/00_acceptance_matrix.md](../acceptance/00_acceptance_matrix.md) |
| MCP 集成指南 | [docs/gateway/02_mcp_integration_cursor.md](./02_mcp_integration_cursor.md) |
