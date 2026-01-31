# 失败降级与补偿（Memory Gateway）

> **术语说明**：Memory Gateway 是 Gateway 组件的完整名称，后续简称 Gateway。详见 [命名规范](../architecture/naming.md)。

> **相关文档**：
> - [根 README §健康检查](../../README.md#健康检查) — 服务健康检查端点
> - [根 README §统一栈验证入口](../../README.md#统一栈验证入口) — 验证命令与脚本
> - [Cursor MCP 集成 §常见错误](02_mcp_integration_cursor.md#step-5-常见错误与排查) — 集成问题排查

目标：OpenMemory 或网络异常时，不阻塞主流程且不丢沉淀。

---

## FULL 门禁语义

在 `FULL` 模式下，所有能力（capabilities）都是必需的，降级行为受到严格约束：

### 核心规则

| 规则 | 说明 |
|------|------|
| **缺能力必须失败** | FULL 模式下，若任一必需能力（如 reconcile）不可用，门禁必须返回失败，不允许静默降级或跳过 |
| **skipped 仅允许显式开关** | 仅当配置中显式设置 `skip_<capability>=true` 时，才允许该能力被跳过；隐式跳过是禁止的 |

### skipped 状态的合法来源

```
allowed_skip_sources:
  - 配置文件中 skip_<capability>=true
  - 环境变量 GATEWAY_SKIP_<CAPABILITY>=1
  - API 请求参数 {"skip": ["capability_name"]}
```

任何其他原因导致的 skipped（如异常捕获后静默跳过）在 FULL 模式下都应视为门禁失败。

### 门禁检查示例

```python
# FULL 模式下的门禁检查逻辑
def gate_check_full_mode(capabilities: dict, config: dict) -> GateResult:
    for cap_name, cap_status in capabilities.items():
        if cap_status == "skipped":
            # 仅允许显式配置的 skip
            if not config.get(f"skip_{cap_name}", False):
                return GateResult(
                    passed=False,
                    reason=f"FULL mode: {cap_name} skipped without explicit config"
                )
        elif cap_status == "failed":
            return GateResult(
                passed=False,
                reason=f"FULL mode: required capability {cap_name} failed"
            )
    return GateResult(passed=True)
```

### 与 DEGRADED 模式的对比

| 模式 | 缺能力行为 | skipped 允许条件 |
|------|------------|------------------|
| **FULL** | 必须失败 | 仅显式开关 |
| **DEGRADED** | 允许降级继续 | 显式开关或异常捕获 |

---

## 写入失败与降级响应

### 降级流程

1. Gateway 生成记忆卡片（payload_md）
2. 调用 OpenMemory 写入失败：
   - 写 Logbook：logbook.outbox_memory(status=pending, last_error=...)
   - 写 Logbook：governance.write_audit(action=redirect, reason=openmemory_write_failed:*)
3. 定时任务或下一次工具启动时 flush outbox：
   - 成功：标记 sent
   - 失败超过阈值：标记 dead 并报警（可选）

### 降级响应契约 (Deferred Response Contract)

当 OpenMemory 不可用时，Gateway 返回 `action=deferred` 的响应，包含以下必需字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | 固定为 `false`（操作未立即完成） |
| `action` | string | 固定为 `"deferred"` |
| `outbox_id` | integer | **必需**，Outbox 队列中的记录 ID |
| `correlation_id` | string | **必需**，请求追踪 ID |
| `space_written` | null | 未写入，为 null |
| `memory_id` | null | 未获得 memory_id，为 null |
| `message` | string | 描述信息，包含错误原因 |

#### 响应示例

```json
{
  "ok": false,
  "action": "deferred",
  "outbox_id": 12345,
  "correlation_id": "corr-abc123def456",
  "space_written": null,
  "memory_id": null,
  "evidence_refs": ["mr://project/123"],
  "message": "OpenMemory 不可用，已入队补偿队列"
}
```

### 审计记录关联

降级时写入的 `governance.write_audit` 记录包含以下关键字段：

| 审计字段 | 值 | 说明 |
|----------|-----|------|
| `action` | `"redirect"` | 表示降级到 outbox |
| `reason` | `"openmemory_write_failed:*"` | 具体错误类型 |
| `evidence_refs_json.outbox_id` | integer | 与响应中的 outbox_id 一致 |
| `evidence_refs_json.extra.correlation_id` | string | 用于追踪关联 |

### 调用方处理指南

```python
# 处理 deferred 响应
if response["action"] == "deferred":
    outbox_id = response["outbox_id"]
    correlation_id = response["correlation_id"]
    
    # 选项 1: 记录并稍后轮询
    log_pending(outbox_id, correlation_id)
    
    # 选项 2: 通知用户
    notify_user("记忆已入队，将在服务恢复后写入")
    
    # 选项 3: 触发异步重试（如有权限）
    schedule_retry(outbox_id)
```

### 契约约束

- **outbox_id 必须返回**：`action=deferred` 时，`outbox_id` 是必需字段，不为 null
- **correlation_id 必须返回**：所有响应都必须包含 `correlation_id`
- **类型稳定性**：`outbox_id` 始终为 integer 类型（或 null），不会返回字符串

详细响应契约定义参见 [能力边界文档](07_capability_boundary.md#统一响应契约-unified-response-contract)。

## 读取失败
- 退回 Logbook：用 analysis.knowledge_candidates 最近结果 + logbook.events 做关键词检索（或 FTS 方案）
- 输出"degraded 模式"标记，提示 Agent 降低依赖历史记忆

## Gateway ↔ Logbook 边界与数据流

### 降级场景中的接口依赖

Gateway 在失败降级场景下依赖的 Logbook 原语接口，详见边界契约文档：

→ **完整接口列表**：[docs/contracts/gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md#logbook-原语接口由-engram_logbook-提供)

→ **降级契约约束**：[docs/contracts/gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md#降级契约)

关键接口概览：
- **治理模块**：`get_or_create_settings`, `insert_write_audit`
- **Outbox 模块**：`enqueue_memory`, `claim_outbox`, `ack_sent`, `fail_retry`, `mark_dead_by_worker`

### 降级数据流

```
写入失败降级:
Gateway → OpenMemory (失败)
       ↓
       → Logbook.insert_write_audit(action=redirect, reason=error)
       → Logbook.outbox_enqueue(payload_md, target_space)

重试流程:
Scheduler → Logbook.outbox_claim_lease()
         ↓
         → OpenMemory.write()
         ├── 成功 → Logbook.outbox_ack_sent()
         └── 失败 → Logbook.outbox_fail_retry()
                 └── 超限 → Logbook.outbox_mark_dead() + 告警

读取失败降级:
Gateway → OpenMemory (失败)
       ↓
       → Logbook.analysis.knowledge_candidates (关键词检索)
       → 返回 {degraded: true, results: [...]}
```

### 接口实现位置

这些原语接口由 `engram_logbook` 包提供，Gateway 通过 `logbook_adapter.py` 适配层调用：

```python
from gateway.logbook_adapter import LogbookAdapter, get_adapter

adapter = get_adapter()
adapter.get_or_create_settings(project_key)
adapter.insert_audit(...)
adapter.enqueue_outbox(...)
```

---

## SCM 同步侧降级与 Gateway/Outbox 交互边界

本节说明 SCM 同步过程中的降级场景，以及与 Gateway/Outbox 的交互边界。

### 职责边界划分

| 组件 | 职责范围 | 降级处理方式 |
|------|----------|--------------|
| **SCM Sync 工具** | 同步 SVN/Git/GitLab 数据到 Logbook | 写入 `scm.*` 表，失败时自行重试或中止 |
| **Gateway** | 将 Logbook 事实转换为记忆卡片，写入 OpenMemory | 失败时使用 `logbook.outbox` 缓冲 |
| **Outbox** | 缓冲待发送记忆，支持异步重试 | 由 Scheduler 消费，超限转死信 |

**核心原则：SCM Sync 不直接与 Outbox 交互**

```
SCM Sync 工具                    Gateway                      OpenMemory
     |                              |                              |
     |-- 写入 scm.* 表 ------------>|                              |
     |   (svn_revisions,            |                              |
     |    git_commits, etc.)        |                              |
     |                              |                              |
     |<---- 同步完成/失败 ----------|                              |
     |   (返回统计，不涉及 outbox)   |                              |
     |                              |                              |
     |                              |-- 转换为记忆卡片 ------------>|
     |                              |   (从 scm.* 读取)             |
     |                              |                              |
     |                              |<---- 写入失败 ----------------|
     |                              |                              |
     |                              |-- 入队 outbox --------------->|
     |                              |   (payload_md, target)       [Logbook]
```

### SCM 同步失败场景

SCM 同步工具在以下场景可能失败：

| 失败类型 | 影响范围 | 处理方式 | 与 Gateway 交互 |
|----------|----------|----------|-----------------|
| **网络超时** | 单次 API 调用 | 内部重试 3 次后中止 | 无 |
| **认证失败** | 整个同步任务 | 立即中止，返回错误 | 无 |
| **数据库写入失败** | 单条或批量记录 | 取决于 strict/best_effort 模式 | 无 |
| **Diff 拉取失败** | 单个 patch_blob | 记录空 URI，后续 materialize | 无 |

**SCM Sync 失败不触发 Outbox**

SCM Sync 的降级处理完全在 Logbook 层面完成：
- 失败时**不更新 watermark**，下次同步自动重试
- 使用 `best_effort` 模式时跳过错误记录继续
- 所有状态存储在 `scm.*` 表和 `logbook.kv`

### Gateway 消费 SCM 数据的降级

Gateway 可能异步消费 `scm.*` 表数据生成记忆卡片。此过程的降级遵循标准 Gateway 降级流程：

```
[定时/触发] Gateway 检测新 SCM 事件
     |
     v
读取 scm.git_commits / scm.svn_revisions (新增记录)
     |
     v
生成记忆卡片 (payload_md)
     |
     v
调用 OpenMemory 写入
     |
     +-- 成功 --> 更新消费 cursor
     |
     +-- 失败 --> 标准降级流程:
                  |
                  +-- insert_write_audit(action=redirect)
                  +-- outbox_enqueue(payload_md)
```

### 交互边界约束

1. **SCM Sync 工具不感知 Outbox**
   - SCM Sync 只负责将数据写入 `scm.*` 表
   - 不关心数据是否被 Gateway 消费或转换为记忆

2. **Gateway 独立消费 SCM 数据**
   - Gateway 维护自己的消费 cursor（可存储在 `logbook.kv`）
   - SCM 数据的消费与 SCM 同步解耦

3. **Outbox 只服务于 Gateway → OpenMemory 链路**
   - Outbox 用于缓冲 Gateway 无法写入 OpenMemory 的记忆
   - 不用于 SCM 同步的重试（SCM 有自己的 watermark 机制）

4. **错误日志分离**
   - SCM Sync 错误：写入 `logbook.events` 或独立日志
   - Gateway 降级错误：写入 `governance.write_audit`

### 消费 Cursor 与 SCM Watermark 的区别

| 概念 | 存储位置 | 用途 | 更新时机 |
|------|----------|------|----------|
| **SCM Watermark** | `logbook.kv (scm.sync/*)` | 标记 SCM 同步进度 | SCM Sync 完成时 |
| **Gateway 消费 Cursor** | `logbook.kv (gateway.consume/*)` | 标记 Gateway 消费进度 | Gateway 处理完成时 |

两者独立维护，互不影响：
- SCM Sync 更新 `svn_cursor:1` → `{"last_rev": 1000}`
- Gateway 消费后更新 `scm_consume:1` → `{"last_processed_rev": 1000}`

这种设计允许：
- SCM 同步可以快速推进，不等待 Gateway 处理
- Gateway 可以独立重放历史 SCM 数据
- 两个组件的失败不互相阻塞

---

## 降级验证

### 验证入口

通过完整验证模式测试降级场景：

```bash
# 主入口：Makefile（推荐）
VERIFY_FULL=1 make verify-unified
```

### 降级测试流程

完整验证模式（`--full`）自动执行以下降级测试：

1. **停止 OpenMemory 容器** — 模拟服务不可用
2. **发送 memory_store 请求** — 验证降级到 Outbox
3. **重启 OpenMemory 容器** — 恢复服务
4. **运行 outbox_worker flush** — 验证重试机制

### 前置条件

| 条件 | 说明 |
|------|------|
| `POSTGRES_DSN` | 必须设置，用于 Outbox 验证 |
| Docker 权限 | 需要能够停止/启动容器 |
| `COMPOSE_PROJECT_NAME` | 推荐设置，用于动态获取容器名 |

### 手动降级测试

```bash
# 1. 停止 OpenMemory
docker stop <openmemory_container>

# 2. 发送写入请求（预期降级到 Outbox）
curl -X POST http://localhost:8787/mcp \
  -H "Content-Type: application/json" \
  -d '{"tool": "memory_store", "arguments": {"payload_md": "test", "target_space": "team:test"}}'

# 3. 检查 Outbox 入队
psql $POSTGRES_DSN -c "SELECT * FROM logbook.outbox_memory WHERE status = 'pending'"

# 4. 重启 OpenMemory
docker start <openmemory_container>

# 5. 运行 outbox_worker
python -m gateway.outbox_worker --once
```

详细验证选项参见 [根 README §统一栈验证入口](../../README.md#统一栈验证入口)。
