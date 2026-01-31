# Outbox Lease Protocol v1

本文档定义 `outbox_memory` 表的 Lease 协议规范，用于多 Worker 并发安全地处理补偿队列。

## 实现与测试链接

- **核心实现**: [`src/engram/logbook/outbox.py`](../../src/engram/logbook/outbox.py)
- **单元测试**: [`tests/logbook/test_outbox_lease.py`](../../tests/logbook/test_outbox_lease.py)
- **Gateway Worker**: [`src/engram/gateway/outbox_worker.py`](../../src/engram/gateway/outbox_worker.py)

---

## 1. 状态机

```
         ┌───────────────────────────────────────┐
         │                                       │
         ▼                                       │
    ┌─────────┐    ack_sent()    ┌──────────┐   │
    │ pending │ ───────────────► │   sent   │   │
    └─────────┘                  └──────────┘   │
         │                                       │
         │  mark_dead()                          │
         │  (retry_count >= max_retries)         │
         ▼                                       │
    ┌─────────┐                                  │
    │  dead   │                                  │
    └─────────┘                                  │
         │                                       │
         └───────────────────────────────────────┘
                    fail_retry()
                (保持 pending，增加 retry_count)
```

### 状态定义

| 状态 | 含义 | 终态 |
|------|------|------|
| `pending` | 待处理，等待 Worker 领取或重试 | 否 |
| `sent` | 已成功写入 OpenMemory | 是 |
| `dead` | 不可恢复的失败（重试耗尽或不可重试错误） | 是 |

---

## 2. 协议函数语义

### 2.1 claim_outbox

```python
def claim_outbox(
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 60,
    config: Optional[Config] = None,
) -> List[Dict[str, Any]]
```

**语义**：并发安全地获取并锁定待处理的 outbox 记录。

**行为**：
1. 选择满足条件的记录：
   - `status = 'pending'`
   - `next_attempt_at <= now()`
   - `locked_at IS NULL` 或 `locked_at < now() - lease_seconds`（租约已过期）
2. 使用 `FOR UPDATE SKIP LOCKED` 跳过已被其他事务锁定的行
3. 更新选中记录的 `locked_by = worker_id`, `locked_at = now()`
4. 立即 COMMIT，返回记录列表

**并发保障**：
- PostgreSQL 行级锁 + SKIP LOCKED 保证多 Worker 不会重复处理同一记录
- 租约过期的记录可被重新领取（防止 Worker 崩溃导致记录卡死）

### 2.2 ack_sent

```python
def ack_sent(
    outbox_id: int,
    worker_id: str,
    memory_id: Optional[str] = None,
    config: Optional[Config] = None,
) -> bool
```

**语义**：确认 outbox 记录已成功发送 (`pending` → `sent`)。

**前置条件**：
- `status = 'pending'`
- `locked_by = worker_id`（验证租约归属）

**行为**：
1. 验证 `locked_by` 匹配当前 `worker_id`
2. 更新 `status = 'sent'`
3. 清除锁：`locked_at = NULL`, `locked_by = NULL`
4. 可选：将 `memory_id` 记录到 `last_error` 字段（用于追踪）

**返回值**：
- `True`：成功更新
- `False`：未更新（锁已被抢占或状态已变更）

### 2.3 fail_retry

```python
def fail_retry(
    outbox_id: int,
    worker_id: str,
    error: str,
    next_attempt_at: Union[datetime, str],
    config: Optional[Config] = None,
) -> bool
```

**语义**：标记 outbox 记录处理失败，安排重试。

**前置条件**：
- `status = 'pending'`
- `locked_by = worker_id`

**行为**：
1. 验证 `locked_by` 匹配当前 `worker_id`
2. 更新 `retry_count = retry_count + 1`
3. 设置 `next_attempt_at`（由调用方计算）
4. 记录 `last_error = error`
5. 释放锁：`locked_at = NULL`, `locked_by = NULL`

**重要**：**退避时间计算由调用方（Gateway Worker）负责**，`fail_retry` 只接收计算好的 `next_attempt_at`。

**返回值**：
- `True`：成功更新
- `False`：未更新（锁冲突）

### 2.4 mark_dead_by_worker

```python
def mark_dead_by_worker(
    outbox_id: int,
    worker_id: str,
    error: str,
    config: Optional[Config] = None,
) -> bool
```

**语义**：标记 outbox 记录为死信 (`pending` → `dead`)。

**适用场景**：
- 重试次数耗尽
- 不可恢复的错误（数据格式错误、目标空间无效等）

**前置条件**：
- `status = 'pending'`
- `locked_by = worker_id`

**行为**：
1. 验证 `locked_by` 匹配当前 `worker_id`
2. 更新 `status = 'dead'`
3. 记录 `last_error = error`
4. 释放锁：`locked_at = NULL`, `locked_by = NULL`

**返回值**：
- `True`：成功更新
- `False`：未更新

### 2.5 renew_lease

```python
def renew_lease(
    outbox_id: int,
    worker_id: str,
    config: Optional[Config] = None,
) -> bool
```

**语义**：续期 Lease 租约，防止处理期间租约过期被其他 Worker 抢占。

**典型使用场景**：
1. 调用 OpenMemory 前续期（长时间网络调用）
2. store 成功后、`ack_sent` 前续期（确保 ack 不会因租约过期失败）

**前置条件**：
- `status = 'pending'`
- `locked_by = worker_id`

**行为**：
1. 验证条件
2. 更新 `locked_at = now()`

**返回值**：
- `True`：成功续期
- `False`：未更新（锁已被抢占或状态已变更）

---

## 3. 幂等性保障

### 3.1 check_dedup

```python
def check_dedup(
    target_space: str,
    payload_sha: str,
    config: Optional[Config] = None,
) -> Optional[Dict[str, Any]]
```

**语义**：检查是否存在已成功写入的重复记录。

**查询条件**：`target_space + payload_sha + status='sent'`

**使用时机**：Worker 处理每条记录前调用，发现重复时直接标记当前记录为 `sent`。

### 3.2 幂等性流程

```
Worker 领取记录 (claim)
      │
      ▼
  ┌──────────────┐
  │ check_dedup  │
  └──────────────┘
      │
      ├── 命中 dedup ───► 直接 ack_sent（复用原 memory_id）
      │
      └── 未命中 ───► 正常写入 OpenMemory
                           │
                           ├── 成功 ───► ack_sent
                           │
                           └── 失败 ───► fail_retry 或 mark_dead
```

---

## 4. 并发约束

### 4.1 锁验证

所有状态变更操作（`ack_sent`、`fail_retry`、`mark_dead_by_worker`、`renew_lease`）都包含 `WHERE locked_by = %s` 条件，确保只有持有租约的 Worker 能操作。

### 4.2 租约过期处理

| 场景 | 行为 |
|------|------|
| Worker A 租约过期 | Worker B 可通过 `claim_outbox` 重新获取记录 |
| Worker A 尝试 ack 但租约已过期 | `ack_sent` 返回 `False` |
| Worker A 发现返回 `False` | 应记录冲突审计，不再继续处理该记录 |

### 4.3 冲突处理

当 `ack_sent`/`fail_retry`/`mark_dead_by_worker` 返回 `False` 时，表示发生冲突：
1. 记录可能已被其他 Worker 处理
2. 状态可能已变更（例如已被标记为 `sent`）

Worker 应：
1. 读取当前记录状态（用于审计）
2. 写入 `outbox_flush_conflict` 审计记录
3. 放弃处理该记录

---

## 5. 退避策略

### 5.1 责任划分

| 层级 | 责任 |
|------|------|
| `engram_logbook.outbox.fail_retry` | 接收 `next_attempt_at` 并存储 |
| Gateway Worker | 计算 `next_attempt_at`（指数退避 + jitter） |

### 5.2 退避计算（Gateway Worker 实现）

```python
def calculate_backoff_with_jitter(
    retry_count: int,
    base_seconds: int,      # 默认 60
    max_seconds: int,       # 默认 3600 (1小时)
    jitter_factor: float    # 默认 0.3
) -> int:
    """
    公式: backoff = min(base * 2^retry, max) * (1 + random(-jitter, +jitter))
    """
    backoff = base_seconds * (2 ** retry_count)
    backoff = min(backoff, max_seconds)
    jitter = random.uniform(-jitter_factor, jitter_factor)
    backoff = backoff * (1 + jitter)
    return max(1, int(backoff))
```

### 5.3 退避示例

| retry_count | 基础退避 | jitter 范围 |
|-------------|----------|-------------|
| 1 | 120s | 84s ~ 156s |
| 2 | 240s | 168s ~ 312s |
| 3 | 480s | 336s ~ 624s |
| 4 | 960s | 672s ~ 1248s |
| 5 | 1920s | 1344s ~ 2496s |
| ≥6 | 3600s (max) | 2520s ~ 4680s |

---

## 6. 表结构要点

```sql
-- outbox_memory 关键字段
outbox_id       SERIAL PRIMARY KEY
status          TEXT DEFAULT 'pending'  -- pending/sent/dead
retry_count     INT DEFAULT 0
next_attempt_at TIMESTAMPTZ DEFAULT now()
locked_at       TIMESTAMPTZ            -- 租约开始时间
locked_by       TEXT                   -- Worker ID
last_error      TEXT                   -- 最后一次错误/成功信息
payload_sha     TEXT                   -- 用于幂等去重
target_space    TEXT                   -- 目标空间
```

---

## 7. 典型 Worker 处理流程

```python
# 1. 领取任务
items = claim_outbox(worker_id, limit=10, lease_seconds=120)

for item in items:
    # 2. 幂等检查
    dedup = check_dedup(item.target_space, item.payload_sha)
    if dedup:
        ack_sent(item.outbox_id, worker_id, dedup.memory_id)
        continue
    
    # 3. 续期（长时间操作前）
    renew_lease(item.outbox_id, worker_id)
    
    # 4. 调用 OpenMemory
    result = openmemory_client.store(item.payload_md, ...)
    
    # 5. 再次续期（确保 ack 不会因租约过期失败）
    renew_lease(item.outbox_id, worker_id)
    
    if result.success:
        # 6a. 成功
        ok = ack_sent(item.outbox_id, worker_id, result.memory_id)
        if not ok:
            handle_conflict(...)  # 冲突处理
    else:
        new_retry = item.retry_count + 1
        if new_retry >= MAX_RETRIES:
            # 6b. 死信
            mark_dead_by_worker(item.outbox_id, worker_id, result.error)
        else:
            # 6c. 重试
            next_at = calculate_backoff(new_retry)
            fail_retry(item.outbox_id, worker_id, result.error, next_at)
```

---

## 8. 错误码

| 错误码 | 含义 |
|--------|------|
| `outbox_flush_success` | 写入成功 |
| `outbox_flush_retry` | 可重试失败，已安排重试 |
| `outbox_flush_dead` | 不可恢复失败，标记为死信 |
| `outbox_flush_conflict` | 租约冲突，本次尝试被忽略 |
| `outbox_flush_dedup_hit` | 幂等检测命中，复用已有记录 |
| `outbox_flush_db_timeout` | 数据库语句超时 |
| `outbox_flush_db_error` | 数据库错误 |

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-01-30 | 初始版本 |
