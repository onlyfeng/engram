# Memory Gateway ↔ Logbook 边界契约

本文档定义 Memory Gateway（MCP 网关）与 Logbook（事实层）之间的接口边界、职责划分与不变量约束。

> **术语说明**：Memory Gateway 是 Gateway 组件的完整名称，后续简称 Gateway。详见 [命名规范](../architecture/naming.md)。

> **相关文档**：
> - [根 README 快速开始](../../README.md) — 部署指南、健康检查、常见问题
> - [Cursor MCP 集成指南](../gateway/02_mcp_integration_cursor.md) — IDE 端配置、端到端集成步骤
> - [Gateway 文档目录](../gateway/README.md) — Gateway 完整文档索引

## 概述

| 组件 | 职责范围 | 核心模块 |
|------|----------|----------|
| **Logbook** | 提供原语接口：治理设置、审计日志、outbox 队列、URI 解析 | `engram_logbook.*` |
| **Gateway** | 负责策略校验、调用协调、补偿流程、与 OpenMemory 交互 | `gateway.*` |

### URI Grammar 归属声明

> **规范所有权**：**URI Grammar 的唯一规范由 Logbook 层定义和维护**。
>
> - **规范文档**：[`src/engram/logbook/uri.py`](../../src/engram/logbook/uri.py) 模块文档
> - **格式定义**：Evidence URI、Artifact Key、Physical URI 的语法规则均由 `engram_logbook.uri` 模块定义
> - **解析实现**：`parse_uri()`、`parse_evidence_uri()`、`parse_attachment_evidence_uri()` 等函数为唯一权威解析器
> - **Gateway 职责**：Gateway 仅调用 Logbook URI 模块进行解析与构建，不自行定义 URI 格式
>
> 任何涉及 URI 格式变更的提案必须在 Logbook 层进行评审和实现。

---

## 契约项总表

| 契约项 | 定义位置 | 实现模块 | 测试归属 | 兼容策略 | 破坏性变更判定 |
|--------|----------|----------|----------|----------|----------------|
| **URI 规范** | [docs/logbook/01_architecture.md](../logbook/01_architecture.md) URI 双轨规范 | `engram_logbook.uri` | `tests/logbook/test_uri_boundary_contract.py` | 新增 scheme 可向后兼容；修改现有 scheme 解析规则需迁移 | 改变 `memory://` 路径结构、`parse_*` 返回字段 |
| **Evidence URI 格式** | [docs/gateway/03_memory_contract.md](../gateway/03_memory_contract.md) Evidence 字段 | `engram_logbook.uri` | `tests/logbook/test_uri_resolution.py` | 新增字段可向后兼容 | 移除必需字段、改变 artifact_uri 格式 |
| **Outbox 状态机** | [src/engram/logbook/outbox.py](../../src/engram/logbook/outbox.py) 模块文档 | `engram_logbook.outbox` | `tests/logbook/test_outbox_lease.py` | 新增状态需文档说明 | 改变 `pending→sent`/`pending→dead` 转换条件 |
| **Outbox Lease 协议** | [src/engram/logbook/outbox.py](../../src/engram/logbook/outbox.py) Lease 协议函数 | `engram_logbook.outbox` | `tests/logbook/test_outbox_lease.py` | Worker 实现需遵循协议 | 改变 `locked_by` 验证逻辑、lease 过期判定 |
| **Governance 设置** | [docs/gateway/04_governance_switch.md](../gateway/04_governance_switch.md) | `engram_logbook.governance` | `tests/gateway/test_audit_event_contract.py` | 新增 policy 字段可向后兼容 | 改变 `team_write_enabled` 默认值或语义 |
| **Write Audit 结构** | [src/engram/logbook/governance.py](../../src/engram/logbook/governance.py) evidence_refs_json 规范 | `engram_logbook.governance` | `tests/gateway/test_audit_event_contract.py` | 新增字段可向后兼容 | 移除 `evidence_refs_json` 必需字段 |
| **Write Audit 原子性** | [ADR: Gateway 审计原子性](../architecture/adr_gateway_audit_atomicity.md) | `engram_logbook.governance` | `tests/gateway/test_audit_event.py` | 见 ADR | 改变 status 状态机语义 |
| **Degradation 策略** | [docs/gateway/05_failure_degradation.md](../gateway/05_failure_degradation.md) | Gateway 实现 | Gateway 测试 | 新增降级模式需文档说明 | 改变 FULL 模式失败语义 |

---

## 边界定义

### Logbook 原语接口（由 `engram_logbook` 提供）

Logbook 只提供**原语级别**的数据操作接口，不包含业务策略逻辑。

#### 1. URI 模块 (`engram_logbook.uri`)

| 函数 | 用途 | Gateway 调用场景 |
|------|------|------------------|
| `parse_uri(uri)` | 解析 URI 结构（scheme, path, type） | 验证 evidence 引用格式 |
| `build_evidence_uri(source_type, source_id, sha256)` | 构建 canonical evidence URI | 生成 write_audit.evidence_refs_json |
| `parse_evidence_uri(uri)` | 解析 patch_blobs evidence URI | 回溯 evidence 原始数据 |
| `parse_attachment_evidence_uri(uri)` | 解析 attachment evidence URI | 回溯附件原始数据 |
| `build_evidence_refs_json(patches, attachments)` | 构建统一的 evidence_refs_json | 写入审计时组装证据 |
| `validate_evidence_ref(ref)` | 验证 evidence reference 结构 | 可选的输入校验 |

**URI 格式规范**：

```
# Artifact Key（DB 存储格式，推荐）
scm/<project_key>/<repo_id>/<source_type>/<rev_or_sha>/<sha256>.<ext>

# Evidence URI（逻辑引用）
memory://patch_blobs/<source_type>/<source_id>/<sha256>
memory://attachments/<attachment_id>/<sha256>

# Physical URI（特例输入）
file://, s3://, https:// 等
```

→ 详见 [docs/logbook/01_architecture.md](../logbook/01_architecture.md) URI 双轨规范

#### 2. Outbox 模块 (`engram_logbook.outbox`)

| 函数 | 用途 | Gateway 调用场景 |
|------|------|------------------|
| `enqueue_memory(payload_md, target_space, item_id, last_error)` | 将记忆入队到 outbox | OpenMemory 写入失败时的降级缓冲 |
| `check_dedup(target_space, payload_sha)` | 幂等去重检查 | 避免重复入队 |
| `claim_outbox(worker_id, limit, lease_seconds)` | 获取待处理记录（Lease 协议） | outbox_worker 批量获取任务 |
| `ack_sent(outbox_id, worker_id, memory_id)` | 确认发送成功 | 重试成功后标记 |
| `fail_retry(outbox_id, worker_id, error, next_attempt_at)` | 标记失败，安排重试 | 可重试错误处理 |
| `mark_dead_by_worker(outbox_id, worker_id, error)` | 标记死信 | 不可恢复错误或重试耗尽 |
| `renew_lease(outbox_id, worker_id)` | 续期租约 | 长时间处理时防止被抢占 |

**Outbox 状态机**：

```
pending ──────────────────────────> sent   (写入成功)
    │                                 
    └──────────────────────────────> dead   (重试耗尽)
```

→ 详见 [docs/gateway/05_failure_degradation.md](../gateway/05_failure_degradation.md) 降级流程

#### 3. Governance 模块 (`engram_logbook.governance`)

| 函数 | 用途 | Gateway 调用场景 |
|------|------|------------------|
| `get_or_create_settings(project_key)` | 获取或创建治理设置 | 写入前检查 team_write_enabled |
| `upsert_settings(project_key, team_write_enabled, policy_json)` | 更新治理设置 | 管理员配置变更 |
| `insert_write_audit(actor, target_space, action, reason, payload_sha, evidence_refs)` | 插入审计记录 | 每次写入操作后记录 |
| `query_write_audit(since, limit, actor, action)` | 查询审计记录 | 生成 reliability_report |

→ 详见 [docs/gateway/04_governance_switch.md](../gateway/04_governance_switch.md) 治理开关

---

### Gateway 职责（由 `gateway` 实现）

Gateway 负责**策略决策**和**流程协调**，调用 Logbook 原语接口但不直接操作数据库。

#### 1. Logbook 适配层 (`gateway.logbook_adapter`)

```python
from gateway.logbook_adapter import LogbookAdapter, get_adapter

adapter = get_adapter()
adapter.get_or_create_settings(project_key)  # → engram_logbook.governance
adapter.insert_audit(...)                     # → engram_logbook.governance
adapter.enqueue_outbox(...)                   # → engram_logbook.outbox
```

**职责**：
- 封装 Logbook 原语调用，提供 Gateway 友好的接口
- 处理配置注入和连接管理
- 不包含业务策略逻辑

#### 2. Outbox Worker (`gateway.outbox_worker`)

**职责**：
- 定时调度 outbox 消费
- 实现 Lease 协议（claim → process → ack/fail）
- 计算重试退避时间
- 判断是否达到死信阈值

**流程**：

```
Scheduler → claim_outbox(worker_id)
         ↓
         → OpenMemory.write()
         ├── 成功 → ack_sent(outbox_id, worker_id, memory_id)
         └── 失败 
             ├── 可重试 → fail_retry(outbox_id, worker_id, error, next_attempt_at)
             └── 超限 → mark_dead_by_worker(outbox_id, worker_id, error) + 告警
```

#### 3. Reconcile 流程 (`gateway.reconcile_outbox`)

**职责**：
- 定期校验 audit ↔ outbox 一致性
- 依赖查询：`evidence_refs_json->>'outbox_id'`
- 处理孤立记录（audit 有但 outbox 缺失）

---

## 关键不变量

以下不变量必须在系统运行过程中始终成立，违反时应触发告警。

> **审计原子性**：当前审计记录可能无法准确反映写入的最终状态（如 OpenMemory 写入失败但审计已记录为 allow）。解决方案见 [ADR: Gateway 审计原子性](../architecture/adr_gateway_audit_atomicity.md)。

### 1. Redirect ↔ Outbox 计数闭环

```sql
-- 每个 redirect 必须有对应的 outbox 记录
SELECT COUNT(*) FROM governance.write_audit WHERE action = 'redirect'
  = SELECT COUNT(*) FROM logbook.outbox_memory 
    WHERE status IN ('pending', 'sent', 'dead')
```

**验证方式**：reconcile_outbox 定期检查

**违反场景**：
- audit 写入成功但 outbox 入队失败（应回滚 audit）
- outbox 记录被意外删除

### 2. Reliability Report 完整性

```sql
reliability_report.total_writes = COUNT(*) FROM governance.write_audit
reliability_report.success_rate = COUNT(action='allow') / COUNT(*)
```

**验证方式**：report 生成时聚合校验

### 3. Evidence URI 可解析性

```sql
-- evidence_refs_json 中的 artifact_uri 必须可被 Logbook 解析
-- 格式: memory://patch_blobs/<source_type>/<source_id>/<sha256>
--       memory://attachments/<attachment_id>/<sha256>
```

**验证方式**：`validate_evidence_ref()` 校验

### 4. Outbox 幂等性保证

```sql
-- 相同 (target_space, payload_sha) 且 status='sent' 的记录视为重复
-- 重复入队时应返回已存在记录而非创建新记录
```

**验证方式**：`check_dedup()` 前置检查

---

## 兼容性策略

### 向后兼容变更（无需迁移）

| 变更类型 | 示例 |
|----------|------|
| 新增 URI scheme | 增加 `gs://` Google Cloud Storage 支持 |
| 新增 evidence_refs_json 字段 | 添加 `size_bytes`、`kind` 等可选字段 |
| 新增 outbox 状态 | 添加 `retrying` 中间状态（需更新查询） |
| 新增 policy_json 字段 | 添加 `max_retries`、`rate_limit` 等策略 |

### 破坏性变更（需迁移计划）

| 变更类型 | 影响 | 迁移要求 |
|----------|------|----------|
| 修改 `memory://` URI 路径结构 | 现有 evidence 引用失效 | 批量更新 DB + 版本化 URI 解析 |
| 移除 `evidence_refs_json` 必需字段 | 查询/校验失败 | 数据补全脚本 |
| 修改 `team_write_enabled` 默认值 | 写入行为变化 | 显式设置所有项目 |
| 修改 Outbox Lease 过期判定 | Worker 行为变化 | 所有 Worker 同步升级 |

---

## 文档链接索引

| 主题 | 文档路径 |
|------|----------|
| URI 双轨规范 | [docs/logbook/01_architecture.md](../logbook/01_architecture.md#uri-双轨规范) |
| Memory Card 格式 | [docs/gateway/03_memory_contract.md](../gateway/03_memory_contract.md#记忆卡片模板推荐-200600-字) |
| Evidence 字段规范 | [docs/gateway/03_memory_contract.md](../gateway/03_memory_contract.md#为何-attachment-uri-必须与-logbook-parse_attachment_evidence_uri-一致) |
| 治理开关 | [docs/gateway/04_governance_switch.md](../gateway/04_governance_switch.md) |
| 失败降级流程 | [docs/gateway/05_failure_degradation.md](../gateway/05_failure_degradation.md) |
| Gateway 设计 | [docs/gateway/06_gateway_design.md](../gateway/06_gateway_design.md) |
| Audit/Outbox 闭环 | [docs/gateway/06_gateway_design.md](../gateway/06_gateway_design.md#audit--outbox--reliability-report-一致性闭环) |
| SCM 同步边界 | [docs/gateway/05_failure_degradation.md](../gateway/05_failure_degradation.md#scm-同步侧降级与-gatewayoutbox-交互边界) |

---

## 降级契约

本节定义 Gateway 在失败降级场景下的强制行为约束。

### 1. 审计写入阻断规则

**Gateway 必须先写审计，失败即阻断主操作。**

| 规则 | 说明 |
|------|------|
| **审计优先** | 任何写入操作必须先调用 `insert_write_audit()`，成功后才能继续主流程 |
| **失败阻断** | 审计写入失败时，Gateway 必须返回错误，不允许继续执行写入操作 |
| **不可跳过** | 即使 OpenMemory 写入成功，若审计写入失败，也应视为整体失败 |

```python
# 正确实现示例
def memory_store(...):
    # 1. 先写审计
    try:
        audit_id = insert_write_audit(action="allow", ...)
    except Exception as e:
        return error(code="AUDIT_WRITE_FAILED", message=str(e))
    
    # 2. 审计成功后才写 OpenMemory
    result = openmemory_write(...)
    return result
```

### 2. Outbox 入队失败处理

**入队失败必须返回可追溯错误，包含 ErrorCode 和上下文。**

| 场景 | 必须返回的信息 |
|------|----------------|
| 数据库连接失败 | `ErrorCode.OUTBOX_ENQUEUE_FAILED` + 连接错误详情 |
| 唯一键冲突（重复入队） | `ErrorCode.OUTBOX_DEDUP_HIT` + 已存在的 `outbox_id` |
| 校验失败 | `ErrorCode.OUTBOX_VALIDATION_FAILED` + 字段级错误 |

返回结构要求：

```python
# 错误返回必须包含以下字段
{
    "success": False,
    "error_code": "OUTBOX_ENQUEUE_FAILED",  # 使用 ErrorCode 枚举
    "message": "数据库写入失败: connection timeout",
    "context": {
        "target_space": "team:project-x",
        "payload_sha": "sha256:abc123...",
        "timestamp": "2026-01-30T10:00:00Z"
    }
}
```

### 3. Reconcile 职责边界

**Reconcile 只修复 audit 缺失与 stale lock，不改写业务结果。**

| 允许的操作 | 禁止的操作 |
|-----------|-----------|
| 补写缺失的 audit 记录 | 修改 outbox 的 status（sent/dead） |
| 清除 stale lock 并重新调度 | 改写 payload_md 或 target_space |
| 写入 `outbox_stale` 审计事件 | 删除任何 outbox 或 audit 记录 |

具体约束：

```
reconcile 允许操作:
  ├── status=sent 且缺少 outbox_flush_success 审计 → 补写审计
  ├── status=dead 且缺少 outbox_flush_dead 审计 → 补写审计
  └── status=pending 且 locked 已过期 → 写 outbox_stale 审计 + 清除锁 + 重新调度

reconcile 禁止操作:
  ├── 将 status=pending 直接改为 sent/dead
  ├── 修改 payload_md/payload_sha/target_space
  └── 删除 outbox_memory 或 write_audit 记录
```

→ 实现参考：[src/engram/gateway/reconcile_outbox.py](../../src/engram/gateway/reconcile_outbox.py)

---

## 测试归属边界

下表列出 Outbox 相关测试文件的职责边界，避免跨层重复测试。

| 测试文件 | 归属层 | 测试职责 | 不测试的内容 |
|----------|--------|----------|--------------|
| `tests/logbook/test_outbox_lease.py` | Logbook | `engram_logbook.outbox` 原语契约：SQL 行为、锁机制、状态转换、Lease 过期判定 | Worker 流程语义、退避计算、OpenMemory 调用形态 |
| `tests/gateway/test_outbox_worker.py` | Gateway | Worker 流程语义：claim/ack/fail 调用形态、退避计算、space 参数传递、审计日志字段 | SQL 实现细节、锁竞争、Lease 过期边界 |
| `tests/gateway/test_outbox_worker_integration.py` | Gateway | 端到端流程验证：真实 DB + mock OpenMemory，状态流转完整性 | SQL 实现细节、锁竞争、Lease 过期边界 |

**设计原则**：
- **Logbook 测试**：验证原语契约的正确性，确保 SQL 行为和锁机制符合预期
- **Gateway 测试**：验证 Worker 如何正确调用原语，以及业务语义（退避、审计等）
- **不重复测试**：Gateway 测试假定 Logbook 原语正确，只 mock 或使用其接口，不重复验证 SQL 边界条件

---

## 附录：接口签名速查

### engram_logbook.uri

```python
def parse_uri(uri: str) -> ParsedUri: ...
def build_evidence_uri(source_type: str, source_id: str, sha256: str) -> str: ...
def parse_evidence_uri(evidence_uri: str) -> Optional[dict]: ...
def parse_attachment_evidence_uri(evidence_uri: str) -> Optional[dict]: ...
def build_evidence_refs_json(patches: Optional[list], attachments: Optional[list], extra: Optional[dict]) -> dict: ...
def validate_evidence_ref(ref: dict) -> tuple[bool, Optional[str]]: ...
```

### engram_logbook.outbox

```python
def enqueue_memory(payload_md: str, target_space: str, item_id: Optional[int], last_error: Optional[str]) -> int: ...
def check_dedup(target_space: str, payload_sha: str) -> Optional[dict]: ...
def claim_outbox(worker_id: str, limit: int, lease_seconds: int) -> list[dict]: ...
def ack_sent(outbox_id: int, worker_id: str, memory_id: Optional[str]) -> bool: ...
def fail_retry(outbox_id: int, worker_id: str, error: str, next_attempt_at: Union[datetime, str]) -> bool: ...
def mark_dead_by_worker(outbox_id: int, worker_id: str, error: str) -> bool: ...
def renew_lease(outbox_id: int, worker_id: str) -> bool: ...
```

### engram_logbook.governance

```python
def get_or_create_settings(project_key: str) -> dict: ...
def upsert_settings(project_key: str, team_write_enabled: bool, policy_json: Optional[dict], updated_by: Optional[str]) -> bool: ...
def insert_write_audit(actor_user_id: Optional[str], target_space: str, action: str, reason: Optional[str], payload_sha: Optional[str], evidence_refs_json: Optional[dict]) -> int: ...
def query_write_audit(since: Optional[str], limit: int, actor: Optional[str], action: Optional[str]) -> list: ...
```
