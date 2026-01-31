# ADR: Gateway 审计原子性方案选型

> 状态: **待定**  
> 创建日期: 2026-01-30  
> 决策者: Engram Core Team

---

## 1. 背景与问题

### 1.1 当前状态

Gateway 的写入流程遵循"审计优先"原则（参见 [gateway_logbook_boundary.md](../contracts/gateway_logbook_boundary.md#降级契约)）：

```
1. 调用 insert_write_audit(action="allow", ...)  ← 先写审计
2. 审计成功后调用 OpenMemory.write()            ← 再写 OpenMemory
3. 写入失败时入队 outbox                        ← 降级缓冲
```

### 1.2 核心问题

**审计记录无法准确反映写入的最终状态**：

| 场景 | 审计记录 | 实际状态 | 问题 |
|------|----------|----------|------|
| OpenMemory 写入成功 | `action=allow` | 成功 | ✓ 一致 |
| OpenMemory 写入失败，入队 outbox | `action=allow` | 待重试 | ✗ 不一致 |
| Outbox 重试成功 | `action=allow` | 成功 | ✗ 缺少 flush 记录关联 |
| Outbox 重试耗尽（dead） | `action=allow` | 失败 | ✗ 严重不一致 |

**影响**：

1. **Reliability Report 计算不准确**：`success_rate = allow_count / total` 会高估实际成功率
2. **审计记录与 Outbox 状态缺乏关联**：无法从单条审计记录追溯其最终结果
3. **reconcile 逻辑复杂**：需要跨表 JOIN 才能获取真实状态

### 1.3 目标

设计一种方案，使审计记录能准确反映写入操作的**最终状态**，同时满足：

- 不破坏现有"审计优先"的阻断语义
- 保持 Reliability Report 的一致性不变量
- 对测试矩阵（HTTP_ONLY / FULL）的影响可控

---

## 2. 方案对比

### 方案 A：扩展 write_audit 支持"预占位 + finalize 更新"

**核心思路**：在 `governance.write_audit` 表中新增字段，支持两阶段写入（预占位 → finalize）。

**Schema 变更**：

```sql
ALTER TABLE governance.write_audit ADD COLUMN correlation_id UUID;
ALTER TABLE governance.write_audit ADD COLUMN status VARCHAR(16) DEFAULT 'pending';
  -- status: pending | success | failed | redirected
ALTER TABLE governance.write_audit ADD COLUMN updated_at TIMESTAMP;
CREATE INDEX idx_write_audit_correlation ON governance.write_audit(correlation_id);
CREATE INDEX idx_write_audit_status ON governance.write_audit(status);
```

**API 变更**：

```python
# 新增 update_write_audit 函数
def update_write_audit(
    correlation_id: UUID,
    status: str,  # success | failed | redirected
    reason_suffix: Optional[str] = None,
    config: Optional[Config] = None,
) -> bool:
    """
    更新审计记录的最终状态
    
    Args:
        correlation_id: 关联 ID（与 insert_write_audit 返回值对应）
        status: 最终状态
        reason_suffix: 追加到原 reason 的后缀
    """
    ...
```

**写入流程变更**：

```python
# 1. 预占位
correlation_id = uuid4()
audit_id = insert_write_audit(
    action="allow",
    status="pending",           # 新增：预占位状态
    correlation_id=correlation_id,  # 新增：关联 ID
    ...
)

# 2. 写入 OpenMemory
try:
    result = openmemory_write(...)
    # 3a. 成功：finalize
    update_write_audit(correlation_id, status="success")
except Exception as e:
    # 3b. 失败：入队 + finalize
    enqueue_outbox(...)
    update_write_audit(correlation_id, status="redirected", reason_suffix=f":outbox:{outbox_id}")
```

**优点**：
- 单表存储，查询简单
- 无需新增表，迁移成本较低
- 审计记录可自包含最终状态

**缺点**：
- 需要两次写操作（insert + update）
- `pending` 状态的审计记录可能因进程崩溃而永久悬挂
- 需要定期清理/超时处理 pending 记录
- status 列引入新的状态机复杂度

---

### 方案 B：新增 write_attempts 表（attempt/final 分离）

**核心思路**：将"写入尝试"与"最终审计"分离，`write_attempts` 记录每次尝试，`write_audit` 仅记录最终结果。

**Schema 变更**：

```sql
CREATE TABLE governance.write_attempts (
    attempt_id SERIAL PRIMARY KEY,
    correlation_id UUID NOT NULL,
    actor_user_id VARCHAR(255),
    target_space VARCHAR(255) NOT NULL,
    payload_sha VARCHAR(255),
    attempt_at TIMESTAMP DEFAULT now(),
    attempt_result VARCHAR(16) NOT NULL,  -- pending | success | failed | redirected
    error_message TEXT,
    evidence_refs_json JSONB DEFAULT '{}',
    UNIQUE (correlation_id, attempt_result)  -- 防止重复 finalize
);

CREATE INDEX idx_write_attempts_correlation ON governance.write_attempts(correlation_id);
CREATE INDEX idx_write_attempts_result ON governance.write_attempts(attempt_result);
```

**API 变更**：

```python
def record_write_attempt(
    correlation_id: UUID,
    target_space: str,
    attempt_result: str,  # pending | success | failed | redirected
    actor_user_id: Optional[str] = None,
    payload_sha: Optional[str] = None,
    error_message: Optional[str] = None,
    evidence_refs_json: Optional[Dict] = None,
    config: Optional[Config] = None,
) -> int:
    """记录一次写入尝试"""
    ...

def finalize_write_audit(
    correlation_id: UUID,
    config: Optional[Config] = None,
) -> int:
    """
    根据 attempts 表计算最终结果，写入 write_audit
    仅当 attempts 中存在 success 或 redirected（非 pending）时才写入
    """
    ...
```

**写入流程变更**：

```python
# 1. 记录尝试开始
correlation_id = uuid4()
record_write_attempt(correlation_id, target_space, attempt_result="pending", ...)

# 2. 写入 OpenMemory
try:
    result = openmemory_write(...)
    # 3a. 成功
    record_write_attempt(correlation_id, target_space, attempt_result="success", ...)
except Exception as e:
    # 3b. 失败
    record_write_attempt(correlation_id, target_space, attempt_result="failed", error_message=str(e), ...)
    enqueue_outbox(...)

# 4. 定期或触发时 finalize
finalize_write_audit(correlation_id)  # 仅统计 final 结果
```

**Reliability Report 统计口径**：

```sql
-- 仅统计 final 结果（write_audit 表）
SELECT 
    COUNT(*) as total,
    COUNT(*) FILTER (WHERE action = 'allow') as success,
    COUNT(*) FILTER (WHERE action = 'redirect') as redirected,
    COUNT(*) FILTER (WHERE action = 'reject') as rejected
FROM governance.write_audit
WHERE created_at >= :since;

-- 不统计 write_attempts（仅用于调试/追溯）
```

**优点**：
- 职责清晰：attempts 记录过程，audit 记录结论
- write_audit 表结构不变，向后兼容
- Reliability Report 只看 audit 表，统计口径明确
- attempts 表可用于详细追溯和重试分析

**缺点**：
- 新增一张表，需要额外维护
- finalize 逻辑需要触发机制（定时/事件）
- attempts 表可能积累大量数据，需要 GC 策略

---

### 方案 C：事务性方案（写入成功后必须同步写审计）

**核心思路**：改变契约为"只有 OpenMemory 写入成功后才写审计"，失败时通过补偿删除保证一致性。

**写入流程变更**：

```python
# 1. 先写 OpenMemory
try:
    result = openmemory_write(...)
    memory_id = result["memory_id"]
except Exception as e:
    # 失败：直接入队 outbox，不写 audit
    enqueue_outbox(...)
    return

# 2. 写入成功后写审计
try:
    insert_write_audit(action="allow", memory_id=memory_id, ...)
except Exception as e:
    # 审计写入失败：需要回滚 OpenMemory 写入
    # OpenMemory 不支持事务/补偿删除 → 不一致
    raise AuditWriteFailedError("审计写入失败，数据可能不一致")
```

**优点**：
- 概念最简单：审计 = 成功记录
- 无需额外状态字段

**缺点**：
- **严重问题**：OpenMemory 不支持原子回滚/补偿删除
- 审计写入失败时会导致"幽灵写入"（OpenMemory 有数据但无审计）
- 违反"审计优先"的阻断原则
- 实际不可落地，除非 OpenMemory 提供事务支持

---

## 3. 方案对比矩阵

| 维度 | 方案 A（预占位+finalize） | 方案 B（attempts 分离） | 方案 C（事务性） |
|------|--------------------------|------------------------|-----------------|
| **Schema 变更** | 中（新增 3 列 + 2 索引） | 高（新增 1 表） | 低（无变更） |
| **API 变更** | 中（新增 update 函数） | 高（新增 2 函数） | 低（流程调整） |
| **一致性保证** | 强（可追溯状态） | 强（职责分离） | 弱（依赖 OpenMemory 事务） |
| **向后兼容** | 中（需迁移填充 status） | 高（audit 表不变） | 低（语义变更） |
| **悬挂记录风险** | 有（pending 可能悬挂） | 有（attempts 可能悬挂） | 无 |
| **实现复杂度** | 中 | 高 | 低（但不可行） |
| **可落地性** | ✓ 可落地 | ✓ 可落地 | ✗ 不可落地 |

---

## 4. 不变量影响分析

### 4.1 现有不变量

```sql
-- 不变量 1: Redirect ↔ Outbox 计数闭环
audit.count(action=redirect) == outbox.count(status in [pending, sent, dead])

-- 不变量 2: Reliability Report 完整性
reliability_report.total_writes == audit.count(*)
reliability_report.success_rate == audit.count(action=allow) / audit.count(*)
```

### 4.2 方案 A 对不变量的影响

| 不变量 | 影响 | 处理方式 |
|--------|------|----------|
| Redirect ↔ Outbox | 需修改：`status=redirected` 的审计才计入 redirect | 查询条件加 `status='redirected' OR status='success'` |
| Reliability Report | 需修改：`success_rate` 应基于 `status=success` | 修改统计 SQL |

**新不变量**：

```sql
-- 不变量 A1: pending 审计必须在超时后被清理或 finalize
audit.count(status='pending' AND created_at < now() - interval '1 hour') == 0
```

### 4.3 方案 B 对不变量的影响

| 不变量 | 影响 | 处理方式 |
|--------|------|----------|
| Redirect ↔ Outbox | 不变：audit 表仍只记录最终结果 | 无需修改 |
| Reliability Report | 不变：统计 audit 表 | 无需修改 |

**新不变量**：

```sql
-- 不变量 B1: attempts 与 audit 的 correlation 一致性
-- 每个已完成的 correlation_id 必须有且仅有一条 audit 记录
SELECT correlation_id FROM write_attempts WHERE attempt_result IN ('success', 'redirected')
  EXCEPT
SELECT correlation_id FROM write_audit
  = EMPTY (允许延迟，但最终一致)
```

---

## 5. 测试矩阵影响

### 5.1 当前测试矩阵

| 测试模式 | 说明 | 涉及组件 |
|----------|------|----------|
| **HTTP_ONLY** | 仅测试 HTTP API，mock 数据库 | Gateway HTTP 层 |
| **FULL** | 真实数据库 + mock OpenMemory | Gateway + Logbook |

### 5.2 方案 A 测试矩阵影响

| 测试文件 | 变更内容 |
|----------|----------|
| `test_governance.py` | 新增 `test_update_write_audit_*` 测试 |
| `test_outbox_worker_integration.py` | 更新 audit 状态断言 |
| `test_reconcile_outbox.py` | 新增 pending 超时清理测试 |
| `test_unified_stack_integration.py` | 更新 audit 字段断言 |

**新增测试场景**：

- [ ] `test_audit_pending_to_success`: pending → success 状态转换
- [ ] `test_audit_pending_to_redirected`: pending → redirected 状态转换
- [ ] `test_audit_pending_timeout_cleanup`: pending 超时清理
- [ ] `test_audit_concurrent_finalize`: 并发 finalize 幂等性

### 5.3 方案 B 测试矩阵影响

| 测试文件 | 变更内容 |
|----------|----------|
| `test_governance.py` | 新增 `test_write_attempts_*`、`test_finalize_*` 测试 |
| `test_outbox_worker_integration.py` | 无变更（audit 表结构不变） |
| `test_reconcile_outbox.py` | 新增 attempts ↔ audit 一致性测试 |
| `test_unified_stack_integration.py` | 新增 attempts 表断言 |

**新增测试场景**：

- [ ] `test_record_attempt_pending`: 记录 pending 尝试
- [ ] `test_record_attempt_success`: 记录 success 尝试
- [ ] `test_finalize_from_success`: 从 success 生成 audit
- [ ] `test_finalize_from_redirected`: 从 redirected 生成 audit
- [ ] `test_finalize_idempotent`: finalize 幂等性
- [ ] `test_attempts_gc`: attempts 表 GC

---

## 6. 迁移成本分析

### 6.1 方案 A 迁移步骤

1. **Phase 1: Schema 迁移**
   ```sql
   -- 新增列（允许 NULL，后续填充）
   ALTER TABLE governance.write_audit ADD COLUMN correlation_id UUID;
   ALTER TABLE governance.write_audit ADD COLUMN status VARCHAR(16);
   ALTER TABLE governance.write_audit ADD COLUMN updated_at TIMESTAMP;
   
   -- 回填历史数据（假定已有记录都是 success）
   UPDATE governance.write_audit SET status = 'success' WHERE status IS NULL;
   
   -- 设置默认值和约束
   ALTER TABLE governance.write_audit ALTER COLUMN status SET DEFAULT 'pending';
   ALTER TABLE governance.write_audit ALTER COLUMN status SET NOT NULL;
   ```

2. **Phase 2: 代码变更**
   - 修改 `engram_logbook.governance` 模块
   - 修改 Gateway 写入流程
   - 更新 Reliability Report 统计 SQL

3. **Phase 3: 测试更新**
   - 更新现有测试断言
   - 新增状态转换测试

### 6.2 方案 B 迁移步骤

1. **Phase 1: Schema 迁移**
   ```sql
   -- 创建新表
   CREATE TABLE governance.write_attempts (...);
   
   -- 创建索引
   CREATE INDEX idx_write_attempts_correlation ON governance.write_attempts(correlation_id);
   ```

2. **Phase 2: 代码变更**
   - 新增 `engram_logbook.governance` 函数
   - 修改 Gateway 写入流程
   - 新增 finalize 触发机制

3. **Phase 3: 测试更新**
   - 新增 attempts 表测试
   - 新增 finalize 测试
   - 新增一致性测试

### 6.3 迁移成本对比

| 维度 | 方案 A | 方案 B |
|------|--------|--------|
| DDL 变更 | 3 列 + 2 索引 | 1 表 + 2 索引 |
| 数据回填 | 需要（填充 status） | 不需要 |
| 代码变更 | 中（1 函数 + 流程） | 高（2 函数 + 流程 + 触发器） |
| 测试变更 | 中 | 高 |
| 回滚难度 | 低（删除列） | 中（删除表 + 代码回滚） |

---

## 7. 推荐方案

### 7.1 决策

**推荐采用方案 A：扩展 write_audit 支持"预占位 + finalize 更新"**

### 7.2 理由

1. **迁移成本较低**：
   - 单表扩展，不引入新的表间依赖
   - 回填逻辑简单（所有历史记录视为 success）
   - 回滚方便（删除新增列即可）

2. **查询简单**：
   - Reliability Report 仍只查 write_audit 表
   - 状态信息自包含，无需跨表 JOIN

3. **与现有契约兼容**：
   - "审计优先"语义不变（先 insert pending，再 update）
   - Outbox 相关不变量可平滑迁移

4. **pending 悬挂问题可控**：
   - 通过定期清理任务（reconcile 扩展）处理
   - 悬挂的 pending 记录数量有限（仅在进程崩溃时产生）

### 7.3 方案 B 适用场景

如果未来需要以下能力，可考虑迁移到方案 B：
- 详细的重试追溯（每次 attempt 独立记录）
- 复杂的重试策略分析
- attempts 数据与 audit 数据不同的保留策略

---

## 8. 落地步骤

### 8.1 阶段划分

| 阶段 | 内容 | 预计工作量 |
|------|------|------------|
| **Phase 1** | Schema 迁移 + governance API 扩展 | 2-3 天 |
| **Phase 2** | Gateway 写入流程改造 | 2-3 天 |
| **Phase 3** | Reliability Report 统计更新 | 1 天 |
| **Phase 4** | 测试矩阵更新 | 2-3 天 |
| **Phase 5** | 文档更新 + reconcile 扩展 | 1-2 天 |

### 8.2 详细任务清单

#### Phase 1: Schema 迁移 + API 扩展

- [ ] 在 `sql/` 目录新增迁移脚本（write_audit_status 相关）
- [ ] 实现 `update_write_audit(correlation_id, status, reason_suffix)` 函数
- [ ] 修改 `insert_write_audit` 支持 `correlation_id` 和 `status` 参数
- [ ] 编写单元测试

#### Phase 2: Gateway 写入流程改造

- [ ] 修改 `src/engram/gateway/main.py` 中的 `memory_store_impl`
- [ ] 新增 `correlation_id` 生成逻辑
- [ ] 调用 `update_write_audit` 更新最终状态
- [ ] 修改 Outbox 入队逻辑，传递 `correlation_id`

#### Phase 3: Reliability Report 统计更新

- [ ] 修改 `reliability_report_impl` 统计 SQL
- [ ] 更新 `success_rate` 计算公式
- [ ] 新增 `pending_count` 指标（可选）

#### Phase 4: 测试矩阵更新

- [ ] 更新 `test_governance.py`
- [ ] 更新 `test_outbox_worker_integration.py`
- [ ] 更新 `test_unified_stack_integration.py`
- [ ] 新增 pending 超时清理测试

#### Phase 5: 文档更新 + reconcile 扩展

- [ ] 更新 `gateway_logbook_boundary.md`
- [ ] 更新 `06_gateway_design.md`
- [ ] 扩展 reconcile 支持 pending 超时检测
- [ ] 更新 ADR 状态为"已批准"

### 8.3 兼容策略

| 阶段 | 版本 | 策略 |
|------|------|------|
| **双写期** | v0.5.x | 新代码使用 status 字段，旧查询兼容 `status IS NULL OR status='success'` |
| **迁移期** | v0.5.x | 回填历史数据 status = 'success' |
| **稳定期** | v0.6.0+ | 所有记录必须有 status 字段 |

---

## 9. 参考文档

- [Gateway Logbook 边界契约](../contracts/gateway_logbook_boundary.md)
- [Gateway 设计](../gateway/06_gateway_design.md)
- [失败降级与补偿](../gateway/05_failure_degradation.md)
- [governance.py 实现](../../src/engram/logbook/governance.py)

---

## 10. 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-01-30 | v1.0 | 初始版本，推荐方案 A |
