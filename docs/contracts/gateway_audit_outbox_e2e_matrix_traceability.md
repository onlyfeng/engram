# Gateway Audit/Outbox E2E 矩阵测试追溯

> **版本**: v1.0  
> **创建日期**: 2026-02-01  
> **状态**: 生效中

---

## 1. 概述

本文档建立 Outbox E2E 场景与测试用例的追溯矩阵，确保：
1. 每个矩阵场景至少有一个主测试覆盖
2. 现有测试的覆盖点清晰列出
3. 缺口明确标识并给出待补测试路径

相关文档：
- [Gateway 审计/证据/关联性端到端契约](./gateway_audit_evidence_correlation_contract.md)
- [Outbox Lease 契约](./outbox_lease_v1.md)

---

## 2. 场景矩阵与主测试映射

### 2.1 Outbox 状态转换矩阵

| 场景 ID | 场景描述 | Outbox 状态 | 审计 reason | 审计 action | 主测试 |
|---------|----------|-------------|-------------|-------------|--------|
| S-01 | Worker flush 成功 | pending → sent | `outbox_flush_success` | allow | `test_outbox_worker_integration.py::TestOutboxWorkerIntegrationSuccess::test_success_path_status_transition` |
| S-02 | Worker flush 成功 (去重命中) | pending → sent | `outbox_flush_dedup_hit` | allow | `test_outbox_worker_integration.py::TestOutboxWorkerIntegrationDedup::test_dedup_path_skips_openmemory_call` |
| S-03 | Worker 重试 | pending → pending | `outbox_flush_retry` | redirect | `test_outbox_worker_integration.py::TestOutboxWorkerIntegrationRetry::test_retry_path_status_and_retry_count` |
| S-04 | 重试耗尽变为 dead | pending → dead | `outbox_flush_dead` | reject | `test_outbox_worker_integration.py::TestOutboxWorkerIntegrationRetry::test_retry_path_becomes_dead_after_max_retries` |
| S-05 | Stale 锁检测 | pending (stale) | `outbox_stale` | redirect | `test_reconcile_outbox.py::TestReconcileStaleRecords::test_detect_stale_locked_record` |
| S-06 | Reconcile 补写 sent | sent (缺失审计) | `outbox_flush_success` | allow | `test_reconcile_outbox.py::TestReconcileSentRecords::test_detect_sent_missing_audit` |
| S-07 | Reconcile 补写 dead | dead (缺失审计) | `outbox_flush_dead` | reject | `test_reconcile_outbox.py::TestReconcileDeadRecords::test_detect_dead_missing_audit` |
| S-08 | Reconcile 补写 stale | pending (stale, 缺失审计) | `outbox_stale` | redirect | `test_reconcile_outbox.py::TestReconcileStaleRecords::test_stale_audit_queryable_after_fix` |

### 2.2 两阶段审计矩阵

| 场景 ID | 场景描述 | 审计 status | 审计 reason | 主测试 |
|---------|----------|-------------|-------------|--------|
| A-01 | Pre-audit 写入 pending | pending | `policy:*` | `test_reconcile_outbox.py::TestPendingAuditTimeoutAndCleanup::test_pending_audit_exists_in_time_window` |
| A-02 | Finalize pending → success | success | 原 reason | `test_reconcile_outbox.py::TestIdempotentFinalizeAudit::test_finalize_pending_to_success_idempotent` |
| A-03 | Finalize pending → redirected | redirected | `{reason}:outbox:{id}` | `test_reconcile_outbox.py::TestIdempotentFinalizeAudit::test_finalize_pending_to_redirected_idempotent` |
| A-04 | Pending 审计超时检测 | pending (stale) | - | `test_reconcile_outbox.py::TestPendingAuditTimeoutAndCleanup::test_pending_audit_stale_detection` |
| A-05 | Worker 审计 status=success | success | `outbox_flush_success` | `test_outbox_worker_integration.py::TestOutboxWorkerAuditStatusField::test_success_audit_has_status_success` |
| A-06 | Worker 审计 status=redirected | redirected | `outbox_flush_retry` | `test_outbox_worker_integration.py::TestOutboxWorkerAuditStatusField::test_retry_audit_has_status_redirected` |
| A-07 | Worker 审计 status=failed | failed | `outbox_flush_dead` | `test_outbox_worker_integration.py::TestOutboxWorkerAuditStatusField::test_dead_audit_has_status_failed` |

### 2.3 降级与恢复矩阵

| 场景 ID | 场景描述 | 主测试 |
|---------|----------|--------|
| D-01 | OpenMemory 不可用→降级入队 | `test_unified_stack_integration.py::TestDegradationFlow::test_degradation_write_to_outbox` |
| D-02 | 降级后 Worker 恢复写入 | `test_unified_stack_integration.py::TestDegradationFlow::test_degradation_recovery_flush` |
| D-03 | 完整降级恢复循环 | `test_unified_stack_integration.py::TestMCPMemoryStoreWithMockDegradation::test_complete_degradation_and_recovery_cycle` |
| D-04 | 降级→Outbox→Flush→审计一致性 | `test_outbox_worker_integration.py::TestOutboxDegradationRecoveryE2E::test_degradation_to_outbox_recovery_flush_audit_consistency` |
| D-05 | 降级→最大重试→Dead 审计一致性 | `test_outbox_worker_integration.py::TestOutboxDegradationRecoveryE2E::test_degradation_max_retries_dead_audit_consistency` |

### 2.4 租约与冲突矩阵

| 场景 ID | 场景描述 | 主测试 |
|---------|----------|--------|
| L-01 | Worker 获取锁 | `test_outbox_worker_integration.py::TestOutboxWorkerIntegrationLocking::test_lock_acquired_during_processing` |
| L-02 | 租约被抢占→冲突 | `test_outbox_worker_integration.py::TestOutboxWorkerIntegrationConflict::test_conflict_when_lease_stolen_by_second_worker` |
| L-03 | 租约过期冲突检测 | `test_outbox_worker_integration.py::TestOutboxWorkerLeaseConflict::test_lease_expired_conflict_detection` |
| L-04 | 租约续期防止抢占 | `test_outbox_worker_integration.py::TestOutboxWorkerLeaseRenewal::test_renew_lease_prevents_reclaim_during_slow_store` |
| L-05 | 冲突不产生重复审计 | `test_outbox_worker_integration.py::TestOutboxWorkerIntegrationConflict::test_no_duplicate_audit_on_conflict` |

---

## 3. 测试覆盖点详情

### 3.1 `test_outbox_worker_integration.py` 覆盖点列表

#### TestOutboxWorkerIntegrationSuccess

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_success_path_status_transition` | status 从 pending→sent, reason=outbox_flush_success, action=allow, evidence_refs_json 顶层字段 |
| `test_success_path_openmemory_called_with_correct_params` | OpenMemory API 参数传递正确性 |

#### TestOutboxWorkerIntegrationRetry

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_retry_path_status_and_retry_count` | status 保持 pending, retry_count 递增, reason=outbox_flush_retry, action=redirect |
| `test_retry_path_becomes_dead_after_max_retries` | retry_count 达到上限后 status→dead, reason=outbox_flush_dead, action=reject |

#### TestOutboxWorkerIntegrationDedup

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_dedup_path_skips_openmemory_call` | 去重命中时不调用 OpenMemory, reason=outbox_flush_dedup_hit, action=allow |
| `test_dedup_path_audit_contains_original_outbox_id` | 去重审计包含 original_outbox_id 字段 |

#### TestOutboxWorkerIntegrationAuditValidation

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_audit_action_reason_convention` | action/reason 命名约定符合 ErrorCode 常量, evidence_refs_json 结构完整性 |

#### TestOutboxWorkerAuditStatusField

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_success_audit_has_status_success` | 审计记录 status='success' |
| `test_retry_audit_has_status_redirected` | 审计记录 status='redirected' |
| `test_dead_audit_has_status_failed` | 审计记录 status='failed' |

#### TestOutboxWorkerFullAcceptance

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_full_acceptance_status_transitions` | 完整状态机验证 |
| `test_full_acceptance_audit_reason_values` | 所有 reason 值符合 ErrorCode |
| `test_full_acceptance_evidence_refs_outbox_id_queryable` | evidence_refs_json->>'outbox_id' SQL 查询契约 |
| `test_full_acceptance_http_only_mode_skip_visible` | HTTP_ONLY_MODE=1 时测试跳过原因可见 |

### 3.2 `test_reconcile_outbox.py` 覆盖点列表

#### TestReconcileSentRecords

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_detect_sent_missing_audit` | status=sent 缺失审计检测, auto_fix 补写, reason=outbox_flush_success |
| `test_sent_report_mode_no_fix` | auto_fix=False 时只报告不修复 |
| `test_sent_audit_queryable_after_fix` | 补写后 evidence_refs_json->>'outbox_id' 可查询 |
| `test_skip_sent_with_audit` | 已有审计的记录被跳过 |

#### TestReconcileDeadRecords

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_detect_dead_missing_audit` | status=dead 缺失审计检测, auto_fix 补写, reason=outbox_flush_dead |
| `test_dead_report_mode_no_fix` | auto_fix=False 时只报告不修复 |
| `test_dead_audit_queryable_after_fix` | 补写后可查询 |
| `test_dead_audit_contains_last_error` | 审计包含 last_error 字段 |

#### TestReconcileStaleRecords

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_detect_stale_locked_record` | stale 锁检测, reason=outbox_stale, action=redirect |
| `test_stale_report_mode_no_fix` | auto_fix=False 时只报告不修复 |
| `test_stale_audit_queryable_after_fix` | 补写后可查询, extra.original_locked_by 字段 |
| `test_stale_reschedule` | reschedule_stale=True 时重新调度 |
| `test_skip_non_stale_pending` | 未超时的 pending 不被识别为 stale |

#### TestReconcileReasonErrorCodeContract

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_reconcile_uses_errorcode_constants_for_*` | reconcile 使用 ErrorCode 常量而非硬编码 |
| `test_all_outbox_errorcodes_defined_in_logbook` | 所有 outbox 相关 ErrorCode 在 logbook.errors 中定义 |
| `test_reconcile_audit_reason_matches_outbox_status` | status→reason 映射一致性 |

#### TestAuditOutboxInvariants

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_redirect_to_outbox_enqueue_audit_invariant` | redirect 决策审计 action=redirect |
| `test_outbox_flush_success_audit_invariant` | sent→outbox_flush_success 映射, evidence_refs_json 顶层字段契约 |
| `test_outbox_flush_dead_audit_invariant` | dead→outbox_flush_dead 映射, extra.last_error 字段 |
| `test_outbox_stale_audit_invariant` | stale→outbox_stale 映射, extra.original_locked_by 字段 |
| `test_full_redirect_outbox_reconcile_loop` | 完整 redirect→outbox→flush→reconcile 闭环 |
| `test_evidence_refs_json_sql_query_contract` | check_audit_exists 函数的 SQL 查询契约 |

### 3.3 `test_unified_stack_integration.py` 覆盖点列表

#### TestDegradationFlow

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_degradation_write_to_outbox` | OpenMemory 不可用时写入 outbox, outbox_id 生成 |
| `test_degradation_recovery_flush` | Worker 恢复后 flush, status→sent |

#### TestMockDegradationFlow

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_mock_degradation_and_recovery` | Mock 降级模拟, 审计链完整性 |
| `test_outbox_id_and_correlation_id_audit_contract` | outbox_id 与 correlation_id 审计关联契约 |

#### TestMCPMemoryStoreE2E

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_mcp_memory_store_success_with_db_assertions` | MCP 成功路径, 数据库状态验证 |
| `test_mcp_memory_store_outbox_on_openmemory_unavailable` | MCP 降级路径, outbox 入队 |
| `test_worker_flush_outbox_and_audit_completion` | Worker flush 完成, 审计完整性 |

#### TestMCPMemoryStoreWithMockDegradation

| 测试方法 | 覆盖点 |
|----------|--------|
| `test_complete_degradation_and_recovery_cycle` | 完整降级恢复循环, status/reason/action 全链路验证 |

---

## 4. HTTP_ONLY_MODE 跳过原因追踪

以下测试在 `HTTP_ONLY_MODE=1` 环境下跳过：

| 测试文件 | 跳过原因 | 备注 |
|----------|----------|------|
| `test_outbox_worker_integration.py::TestOutboxWorkerFullAcceptance::test_full_acceptance_http_only_mode_skip_visible` | HTTP_ONLY_MODE 不支持 outbox 操作 | 有明确跳过消息 |
| `test_reconcile_outbox.py::TestReconcileSmokeTest::*` | FULL profile required | 需要真实数据库 |
| `test_unified_stack_integration.py::TestDegradationFlow::*` | 需要 OpenMemory 模拟降级 | 依赖外部服务 |

---

## 5. 覆盖缺口与待补测试

### 5.1 已识别缺口

| 缺口 ID | 缺口描述 | 待补测试路径 | 优先级 |
|---------|----------|--------------|--------|
| G-01 | 数据库超时时 outbox 状态一致性 | `tests/gateway/test_outbox_worker_integration.py::TestOutboxWorkerDatabaseTimeout` | P1 - 已有基础测试，需补充 evidence_refs_json 验证 |
| G-02 | 并发 Worker 处理同一 outbox 冲突审计 | `tests/gateway/test_outbox_worker_integration.py::TestOutboxWorkerIntegrationConflict` | P1 - 已有基础测试，需补充审计幂等性验证 |
| G-03 | Reconcile 在 HTTP_ONLY_MODE 下的行为 | `tests/gateway/test_reconcile_outbox.py::TestReconcileHTTPOnlyMode` (待创建) | P2 |
| G-04 | evidence_refs_json 顶层字段变更回归 | `tests/gateway/test_audit_event_contract.py::TestEvidenceRefsJsonTopLevelContract` (待创建) | P1 |
| G-05 | 两阶段审计 pending→failed 路径 | `tests/gateway/test_reconcile_outbox.py::TestTwoPhaseAuditFailedPath` (待创建) | P2 |

### 5.2 待补测试清单

#### G-01: 数据库超时时 evidence_refs_json 验证

```python
# tests/gateway/test_outbox_worker_integration.py

class TestOutboxWorkerDatabaseTimeout:
    def test_db_timeout_audit_contains_evidence_refs_json(self):
        """
        验证数据库超时场景的审计记录包含完整的 evidence_refs_json
        
        覆盖点:
        - evidence_refs_json.outbox_id 存在
        - evidence_refs_json.source = 'outbox_worker'
        - evidence_refs_json.gateway_event.extra.db_error 存在
        """
        pass  # TODO: 实现
```

#### G-03: HTTP_ONLY_MODE Reconcile 行为

```python
# tests/gateway/test_reconcile_outbox.py

class TestReconcileHTTPOnlyMode:
    """HTTP_ONLY_MODE=1 时 reconcile 行为测试"""
    
    def test_reconcile_skips_in_http_only_mode(self):
        """验证 HTTP_ONLY_MODE=1 时 reconcile 优雅跳过"""
        pass  # TODO: 实现
    
    def test_reconcile_skip_reason_logged(self):
        """验证跳过原因被正确记录"""
        pass  # TODO: 实现
```

#### G-04: evidence_refs_json 顶层字段契约测试

```python
# tests/gateway/test_audit_event_contract.py

class TestEvidenceRefsJsonTopLevelContract:
    """evidence_refs_json 顶层字段契约回归测试"""
    
    @pytest.mark.parametrize("scenario,expected_fields", [
        ("outbox_flush_success", ["outbox_id", "source", "memory_id"]),
        ("outbox_flush_dead", ["outbox_id", "source", "extra"]),
        ("outbox_stale", ["outbox_id", "source", "extra"]),
    ])
    def test_top_level_fields_exist(self, scenario, expected_fields):
        """验证各场景的顶层必需字段存在"""
        pass  # TODO: 实现
```

#### G-05: 两阶段审计 pending→failed 路径

```python
# tests/gateway/test_reconcile_outbox.py

class TestTwoPhaseAuditFailedPath:
    """两阶段审计 pending→failed 路径测试"""
    
    def test_finalize_pending_to_failed(self):
        """验证 pending 审计可以 finalize 到 failed 状态"""
        pass  # TODO: 实现
    
    def test_failed_audit_contains_error_details(self):
        """验证 failed 审计包含错误详情"""
        pass  # TODO: 实现
```

---

## 6. 快速验证命令

### 6.1 按场景验证

```bash
# 验证 Outbox 状态转换 (S-01 ~ S-08)
pytest tests/gateway/test_outbox_worker_integration.py \
       tests/gateway/test_reconcile_outbox.py \
       -k "status_transition or missing_audit or stale" -v

# 验证两阶段审计 (A-01 ~ A-07)
pytest tests/gateway/test_reconcile_outbox.py \
       -k "pending or finalize or status_" -v

# 验证降级与恢复 (D-01 ~ D-05)
pytest tests/gateway/test_unified_stack_integration.py \
       tests/gateway/test_outbox_worker_integration.py \
       -k "degradation or recovery" -v

# 验证租约与冲突 (L-01 ~ L-05)
pytest tests/gateway/test_outbox_worker_integration.py \
       -k "lock or conflict or lease" -v
```

### 6.2 完整矩阵验证

```bash
# 一键验证所有 Outbox E2E 场景
pytest tests/gateway/test_outbox_worker_integration.py \
       tests/gateway/test_reconcile_outbox.py \
       tests/gateway/test_unified_stack_integration.py \
       -v --tb=short
```

---

## 7. 变更日志

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-02-01 | v1.0 | 初始版本：建立 E2E 矩阵测试追溯，列出主测试、覆盖点、缺口 |
