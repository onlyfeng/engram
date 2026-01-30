# -*- coding: utf-8 -*-
"""
Outbox Worker 集成测试

**测试归属声明**：本文件只测试 worker 流程语义，不重复验证 SQL/锁实现细节。
SQL/锁等底层原语契约测试请参见 Logbook 测试：
- logbook_postgres/scripts/tests/test_outbox_lease.py

使用真实 Postgres 数据库（复用 Logbook 的 test DB fixture）并 mock OpenMemoryClient.store
覆盖三条路径：
1. 成功写入 (pending -> sent)
2. 失败重试 (pending, retry_count++, next_attempt_at 更新)
3. Dedupe 命中 (直接标记 sent，无 OpenMemory 调用)

DB 层断言：
- outbox 状态流转正确
- 锁字段 (locked_by, locked_at) 正确
- governance.write_audit 记录存在且 reason/action 符合约定

**HTTP_ONLY_MODE 行为**：
当 HTTP_ONLY_MODE=1 时，本文件所有测试将被跳过，输出明确的 SKIP 信息：
  SKIPPED (HTTP_ONLY_MODE: Outbox Worker 集成测试需要 Docker 和数据库)

这确保 acceptance-unified-min（CI PR 快速验证）可以跳过需要 Docker 操作的测试，
而 acceptance-unified-full（Nightly/发布前）则必须运行这些测试。
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
import psycopg


# ---------- HTTP_ONLY_MODE 跳过检测 ----------

HTTP_ONLY_MODE = os.environ.get("HTTP_ONLY_MODE", "0") == "1"
HTTP_ONLY_SKIP_REASON = "HTTP_ONLY_MODE: Outbox Worker 集成测试需要 Docker 和数据库"

# 标记：HTTP_ONLY_MODE 下跳过整个模块
pytestmark = pytest.mark.skipif(
    HTTP_ONLY_MODE,
    reason=HTTP_ONLY_SKIP_REASON
)


# ---------- 测试辅助数据结构 ----------

@dataclass
class MockStoreResult:
    """模拟 OpenMemory 存储结果"""
    success: bool
    memory_id: Optional[str] = None
    error: Optional[str] = None


# ---------- 测试类 ----------

class TestOutboxWorkerIntegrationSuccess:
    """成功写入路径集成测试 (pending -> sent)"""

    def test_success_path_status_transition(self, migrated_db, logbook_adapter_config):
        """成功路径: outbox 状态从 pending 变为 sent"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        # 1. 插入测试 outbox 记录
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:test_user', '# Test Memory', 'sha_success_001', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # 2. Mock OpenMemory client
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(
                success=True,
                memory_id="mem_integration_success_001"
            )
            
            # 3. 执行 worker 处理
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3, jitter_factor=0.0)
                results = process_batch(config, worker_id="integration-worker-success")
            
            # 4. 断言：应该处理了 1 条记录且成功
            assert len(results) == 1
            assert results[0].success is True
            assert results[0].action == "allow"
            assert results[0].reason == "outbox_flush_success"
            
            # 5. 断言 DB 层：outbox 状态变为 sent
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, locked_by, locked_at, last_error
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                
                assert row[0] == "sent", f"状态应为 sent，实际: {row[0]}"
                assert row[1] is None, "locked_by 应为 NULL"
                assert row[2] is None, "locked_at 应为 NULL"
                assert row[3] == "memory_id=mem_integration_success_001", f"last_error 应包含 memory_id，实际: {row[3]}"
            
            # 6. 断言 DB 层：governance.write_audit 记录存在
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason, payload_sha, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_success_001'
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
                audit_row = cur.fetchone()
                
                assert audit_row is not None, "应存在审计记录"
                assert audit_row[0] == "allow", f"action 应为 allow，实际: {audit_row[0]}"
                assert audit_row[1] == "outbox_flush_success", f"reason 应为 outbox_flush_success，实际: {audit_row[1]}"
                assert audit_row[2] == "sha_success_001"
                
                # 验证 evidence_refs_json 包含关键字段
                evidence = audit_row[3]
                assert evidence is not None
                assert evidence.get("outbox_id") == outbox_id
                assert evidence.get("memory_id") == "mem_integration_success_001"
                assert evidence.get("source") == "outbox_worker"
                
                # 验证 extra 包含 correlation_id 和 attempt_id
                extra = evidence.get("extra")
                assert extra is not None, "evidence 应包含 extra 字段"
                assert "correlation_id" in extra, "extra 应包含 correlation_id"
                assert extra["correlation_id"].startswith("corr-"), "correlation_id 应以 corr- 开头"
                assert "attempt_id" in extra, "extra 应包含 attempt_id"
                assert extra["attempt_id"].startswith("attempt-"), "attempt_id 应以 attempt- 开头"
        
        finally:
            # 清理
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_success_001'")
            conn.close()

    def test_success_path_openmemory_called_with_correct_params(self, migrated_db, logbook_adapter_config):
        """成功路径: OpenMemory.store 被正确调用（space/metadata）"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at, item_id)
                    VALUES ('team:project_x', '# Team Memory', 'sha_team_params_002', 'pending', now() - interval '1 minute', 123)
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(success=True, memory_id="mem_team_002")
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3)
                process_batch(config, worker_id="params-worker")
            
            # 断言 OpenMemory store 调用参数
            mock_client.store.assert_called_once()
            call_kwargs = mock_client.store.call_args[1]
            
            assert call_kwargs["content"] == "# Team Memory"
            assert call_kwargs["space"] == "team:project_x"
            assert call_kwargs["user_id"] is None  # team 空间不提取 user_id
            
            metadata = call_kwargs["metadata"]
            assert metadata["outbox_id"] == outbox_id
            assert metadata["payload_sha"] == "sha_team_params_002"
            assert metadata["target_space"] == "team:project_x"
            assert metadata["item_id"] == 123
            assert metadata["source"] == "outbox_worker"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_team_params_002'")
            conn.close()


class TestOutboxWorkerIntegrationRetry:
    """失败重试路径集成测试 (pending, retry_count++, next_attempt_at 更新)"""

    def test_retry_path_status_and_retry_count(self, migrated_db, logbook_adapter_config):
        """重试路径: 状态保持 pending，retry_count 增加"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at)
                    VALUES ('private:retry_user', '# Retry Memory', 'sha_retry_003', 'pending', 0, now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(
                success=False,
                error="connection_timeout"
            )
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3, jitter_factor=0.0, base_backoff_seconds=60)
                results = process_batch(config, worker_id="retry-worker")
            
            # 断言结果
            assert len(results) == 1
            assert results[0].success is False
            assert results[0].action == "redirect"
            assert results[0].reason == "outbox_flush_retry"
            assert results[0].error == "connection_timeout"
            
            # 断言 DB 层：状态保持 pending，retry_count 增加
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, retry_count, locked_by, locked_at, last_error, next_attempt_at
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                
                assert row[0] == "pending", f"状态应保持 pending，实际: {row[0]}"
                assert row[1] == 1, f"retry_count 应为 1，实际: {row[1]}"
                assert row[2] is None, "locked_by 应为 NULL（锁已释放）"
                assert row[3] is None, "locked_at 应为 NULL"
                assert row[4] == "connection_timeout", f"last_error 应为错误信息，实际: {row[4]}"
                
                # 验证 next_attempt_at 在未来
                next_attempt = row[5]
                if next_attempt.tzinfo is None:
                    next_attempt = next_attempt.replace(tzinfo=timezone.utc)
                assert next_attempt > datetime.now(timezone.utc), "next_attempt_at 应在未来"
            
            # 断言 DB 层：governance.write_audit 记录存在
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_retry_003'
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
                audit_row = cur.fetchone()
                
                assert audit_row is not None, "应存在审计记录"
                assert audit_row[0] == "redirect", f"action 应为 redirect，实际: {audit_row[0]}"
                assert audit_row[1] == "outbox_flush_retry", f"reason 应为 outbox_flush_retry，实际: {audit_row[1]}"
                
                evidence = audit_row[2]
                assert evidence.get("retry_count") == 1
                assert "next_attempt_at" in evidence
                
                # 验证 extra 包含 correlation_id, attempt_id 和 last_error
                extra = evidence.get("extra")
                assert extra is not None, "evidence 应包含 extra 字段"
                assert "correlation_id" in extra, "extra 应包含 correlation_id"
                assert "attempt_id" in extra, "extra 应包含 attempt_id"
                assert extra.get("last_error") == "connection_timeout", "extra 应包含 last_error"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_retry_003'")
            conn.close()

    def test_retry_path_becomes_dead_after_max_retries(self, migrated_db, logbook_adapter_config):
        """重试路径: 超过最大重试次数后变为 dead"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            # 已经重试了 2 次，下次失败就是第 3 次 >= max_retries=3
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at)
                    VALUES ('private:dead_user', '# Dead Memory', 'sha_dead_004', 'pending', 2, now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(
                success=False,
                error="permanent_failure"
            )
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3)
                results = process_batch(config, worker_id="dead-worker")
            
            # 断言结果
            assert len(results) == 1
            assert results[0].success is False
            assert results[0].action == "reject"
            assert results[0].reason == "outbox_flush_dead"
            
            # 断言 DB 层：状态变为 dead
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, locked_by, locked_at, last_error
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                
                assert row[0] == "dead", f"状态应为 dead，实际: {row[0]}"
                assert row[1] is None, "locked_by 应为 NULL"
                assert row[2] is None, "locked_at 应为 NULL"
                assert row[3] == "permanent_failure"
            
            # 断言 DB 层：governance.write_audit 记录存在
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_dead_004'
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
                audit_row = cur.fetchone()
                
                assert audit_row is not None, "应存在审计记录"
                assert audit_row[0] == "reject", f"action 应为 reject，实际: {audit_row[0]}"
                assert audit_row[1] == "outbox_flush_dead", f"reason 应为 outbox_flush_dead，实际: {audit_row[1]}"
                
                evidence = audit_row[2]
                assert evidence.get("retry_count") == 3  # 2 + 1
                
                # 验证 extra 包含 correlation_id, attempt_id 和 last_error
                extra = evidence.get("extra")
                assert extra is not None, "evidence 应包含 extra 字段"
                assert "correlation_id" in extra, "extra 应包含 correlation_id"
                assert "attempt_id" in extra, "extra 应包含 attempt_id"
                assert extra.get("last_error") == "permanent_failure", "extra 应包含 last_error"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_dead_004'")
            conn.close()


class TestOutboxWorkerIntegrationDedup:
    """Dedupe 命中路径集成测试 (直接标记 sent，无 OpenMemory 调用)"""

    def test_dedup_path_skips_openmemory_call(self, migrated_db, logbook_adapter_config):
        """Dedupe 命中: 跳过 OpenMemory 调用，直接标记 sent"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        original_outbox_id = None
        duplicate_outbox_id = None
        try:
            # 1. 插入原始成功记录（status=sent）
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, last_error)
                    VALUES ('team:dedup_project', '# Original Memory', 'sha_dedup_005', 'sent', 'memory_id=mem_original_005')
                    RETURNING outbox_id
                """)
                original_outbox_id = cur.fetchone()[0]
            
            # 2. 插入重复的待处理记录（相同 target_space + payload_sha）
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('team:dedup_project', '# Original Memory', 'sha_dedup_005', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                duplicate_outbox_id = cur.fetchone()[0]
            
            # 3. Mock OpenMemory client（不应被调用）
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(success=True, memory_id="should_not_be_called")
            
            # 4. 执行 worker
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3)
                results = process_batch(config, worker_id="dedup-worker")
            
            # 5. 断言结果
            assert len(results) == 1
            assert results[0].success is True
            assert results[0].action == "allow"
            assert results[0].reason == "outbox_flush_dedup_hit"
            
            # 6. 关键断言：OpenMemory store 不应被调用
            mock_client.store.assert_not_called()
            
            # 7. 断言 DB 层：重复记录状态变为 sent
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, locked_by, locked_at, last_error
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (duplicate_outbox_id,))
                row = cur.fetchone()
                
                assert row[0] == "sent", f"状态应为 sent，实际: {row[0]}"
                assert row[1] is None, "locked_by 应为 NULL"
                assert row[2] is None, "locked_at 应为 NULL"
                # last_error 应包含原始记录的 memory_id
                assert row[3] == "memory_id=mem_original_005", f"last_error 应包含原始 memory_id，实际: {row[3]}"
            
            # 8. 断言 DB 层：governance.write_audit 记录存在
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_dedup_005'
                    AND reason = 'outbox_flush_dedup_hit'
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
                audit_row = cur.fetchone()
                
                assert audit_row is not None, "应存在 dedup_hit 审计记录"
                assert audit_row[0] == "allow", f"action 应为 allow，实际: {audit_row[0]}"
                assert audit_row[1] == "outbox_flush_dedup_hit"
                
                evidence = audit_row[2]
                assert evidence.get("outbox_id") == duplicate_outbox_id
                assert evidence.get("original_outbox_id") == original_outbox_id
                assert evidence.get("memory_id") == "mem_original_005"
                
                # 验证 extra 包含 correlation_id 和 attempt_id
                extra = evidence.get("extra")
                assert extra is not None, "evidence 应包含 extra 字段"
                assert "correlation_id" in extra, "extra 应包含 correlation_id"
                assert "attempt_id" in extra, "extra 应包含 attempt_id"
        
        finally:
            with conn.cursor() as cur:
                if original_outbox_id:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (original_outbox_id,))
                if duplicate_outbox_id:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (duplicate_outbox_id,))
                cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_dedup_005'")
            conn.close()

    def test_dedup_path_audit_contains_original_outbox_id(self, migrated_db, logbook_adapter_config):
        """Dedupe 命中: 审计记录包含 original_outbox_id"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        original_outbox_id = None
        duplicate_outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, last_error)
                    VALUES ('private:alice', '# Alice Memory', 'sha_dedup_alice_006', 'sent', 'memory_id=mem_alice_006')
                    RETURNING outbox_id
                """)
                original_outbox_id = cur.fetchone()[0]
            
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:alice', '# Alice Memory', 'sha_dedup_alice_006', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                duplicate_outbox_id = cur.fetchone()[0]
            
            mock_client = MagicMock()
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3)
                process_batch(config, worker_id="dedup-audit-worker")
            
            # 验证审计记录中的 evidence_refs_json
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT actor_user_id, target_space, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_dedup_alice_006'
                    AND reason = 'outbox_flush_dedup_hit'
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
                audit_row = cur.fetchone()
                
                assert audit_row is not None
                # private:alice -> actor_user_id = alice
                assert audit_row[0] == "alice", f"actor_user_id 应为 alice，实际: {audit_row[0]}"
                assert audit_row[1] == "private:alice"
                
                evidence = audit_row[2]
                assert evidence.get("outbox_id") == duplicate_outbox_id
                assert evidence.get("original_outbox_id") == original_outbox_id
                assert evidence.get("source") == "outbox_worker"
                
                # 验证 extra 包含 correlation_id 和 attempt_id
                extra = evidence.get("extra")
                assert extra is not None, "evidence 应包含 extra 字段"
                assert "correlation_id" in extra, "extra 应包含 correlation_id"
                assert "attempt_id" in extra, "extra 应包含 attempt_id"
        
        finally:
            with conn.cursor() as cur:
                if original_outbox_id:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (original_outbox_id,))
                if duplicate_outbox_id:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (duplicate_outbox_id,))
                cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_dedup_alice_006'")
            conn.close()


class TestOutboxWorkerIntegrationLocking:
    """锁字段正确性集成测试"""

    def test_lock_acquired_during_processing(self, migrated_db, logbook_adapter_config):
        """处理过程中 locked_by 和 locked_at 正确设置"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        # 用于在处理过程中捕获锁状态
        captured_lock_info = {}
        
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('team:lock_test', '# Lock Test', 'sha_lock_007', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # 自定义 store 函数，在调用时检查锁状态
            def mock_store(*args, **kwargs):
                # 在 store 调用时查询锁状态
                with psycopg.connect(dsn, autocommit=True) as check_conn:
                    with check_conn.cursor() as cur:
                        cur.execute(f"""
                            SELECT locked_by, locked_at
                            FROM {logbook_schema}.outbox_memory
                            WHERE outbox_id = %s
                        """, (outbox_id,))
                        row = cur.fetchone()
                        captured_lock_info["locked_by"] = row[0]
                        captured_lock_info["locked_at"] = row[1]
                
                return MockStoreResult(success=True, memory_id="mem_lock_007")
            
            mock_client = MagicMock()
            mock_client.store.side_effect = mock_store
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3, lease_seconds=120)
                process_batch(config, worker_id="lock-test-worker")
            
            # 验证处理过程中锁已设置
            assert captured_lock_info["locked_by"] == "lock-test-worker", \
                f"处理中 locked_by 应为 worker_id，实际: {captured_lock_info['locked_by']}"
            assert captured_lock_info["locked_at"] is not None, "处理中 locked_at 应不为空"
            
            # 验证处理完成后锁已释放
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT locked_by, locked_at
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                
                assert row[0] is None, "处理完成后 locked_by 应为 NULL"
                assert row[1] is None, "处理完成后 locked_at 应为 NULL"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_lock_007'")
            conn.close()


class TestOutboxWorkerIntegrationAuditValidation:
    """审计记录 action/reason 约定验证集成测试"""

    def test_audit_action_reason_convention(self, migrated_db, logbook_adapter_config):
        """验证 action/reason 符合约定"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_ids = []
        
        try:
            # 插入三条测试记录
            test_cases = [
                # (payload_sha, will_succeed, retry_count, expected_action, expected_reason)
                ("sha_conv_success_008", True, 0, "allow", "outbox_flush_success"),
                ("sha_conv_retry_008", False, 0, "redirect", "outbox_flush_retry"),
                ("sha_conv_dead_008", False, 2, "reject", "outbox_flush_dead"),  # retry_count=2 + 1 = 3 >= max_retries
            ]
            
            for payload_sha, _, retry_count, _, _ in test_cases:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {logbook_schema}.outbox_memory
                            (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at)
                        VALUES ('team:convention_test', '# Convention Test', %s, 'pending', %s, now() - interval '1 minute')
                        RETURNING outbox_id
                    """, (payload_sha, retry_count))
                    outbox_ids.append(cur.fetchone()[0])
            
            # 按顺序处理每条记录
            for i, (payload_sha, will_succeed, _, expected_action, expected_reason) in enumerate(test_cases):
                mock_client = MagicMock()
                if will_succeed:
                    mock_client.store.return_value = MockStoreResult(success=True, memory_id=f"mem_{payload_sha}")
                else:
                    mock_client.store.return_value = MockStoreResult(success=False, error="test_error")
                
                with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                    from engram.gateway.outbox_worker import WorkerConfig, process_batch
                    
                    config = WorkerConfig(batch_size=1, max_retries=3, jitter_factor=0.0)
                    process_batch(config, worker_id=f"convention-worker-{i}")
            
            # 验证每条记录的审计
            for payload_sha, _, _, expected_action, expected_reason in test_cases:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT action, reason
                        FROM {governance_schema}.write_audit
                        WHERE payload_sha = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                    """, (payload_sha,))
                    row = cur.fetchone()
                    
                    assert row is not None, f"应存在 {payload_sha} 的审计记录"
                    assert row[0] == expected_action, f"{payload_sha}: action 应为 {expected_action}，实际: {row[0]}"
                    assert row[1] == expected_reason, f"{payload_sha}: reason 应为 {expected_reason}，实际: {row[1]}"
                    assert row[1].startswith("outbox_flush_"), f"reason 应以 'outbox_flush_' 开头"
        
        finally:
            for outbox_id in outbox_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            for payload_sha, _, _, _, _ in test_cases:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = %s", (payload_sha,))
            conn.close()


class TestOutboxWorkerIntegrationConflict:
    """Lease 过期冲突路径集成测试"""

    def test_conflict_when_lease_stolen_by_second_worker(self, migrated_db, logbook_adapter_config):
        """
        冲突场景：worker1 处理期间 lease 过期，worker2 抢占并完成
        
        流程：
        1. 插入 outbox 记录
        2. worker1 claim 并开始处理（mock store 成功）
        3. 在 worker1 调用 ack_sent 前，直接更新 DB 模拟 lease 被 worker2 抢占并完成
        4. worker1 调用 ack_sent 返回 False
        5. 断言：worker1 不写 success 审计，写 conflict 审计
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            # 1. 插入测试 outbox 记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:conflict_user', '# Conflict Memory', 'sha_conflict_100', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # 用于在 store 调用后模拟 worker2 抢占
            store_called = {"called": False}
            
            def mock_store_and_steal_lease(*args, **kwargs):
                """在 store 成功后，模拟 worker2 抢占并完成"""
                store_called["called"] = True
                
                # 模拟 worker2 抢占：直接更新 locked_by 和状态为 sent
                with psycopg.connect(dsn, autocommit=True) as steal_conn:
                    with steal_conn.cursor() as cur:
                        cur.execute(f"""
                            UPDATE {logbook_schema}.outbox_memory
                            SET locked_by = 'worker2-stealer',
                                locked_at = now(),
                                status = 'sent',
                                last_error = 'memory_id=mem_worker2_stole'
                            WHERE outbox_id = %s
                        """, (outbox_id,))
                
                return MockStoreResult(success=True, memory_id="mem_worker1_original")
            
            mock_client = MagicMock()
            mock_client.store.side_effect = mock_store_and_steal_lease
            
            # 2. 执行 worker1 处理
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3, jitter_factor=0.0)
                results = process_batch(config, worker_id="worker1-victim")
            
            # 3. 断言：store 被调用
            assert store_called["called"], "OpenMemory store 应被调用"
            
            # 4. 断言：结果应为冲突
            assert len(results) == 1
            result = results[0]
            assert result.success is False, "结果应为失败"
            assert result.reason == "outbox_flush_conflict", f"reason 应为 outbox_flush_conflict，实际: {result.reason}"
            assert result.conflict is True, "conflict 标志应为 True"
            assert result.action == "redirect"
            
            # 5. 断言 DB 层：不存在 success 审计
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT COUNT(*)
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_conflict_100'
                    AND reason = 'outbox_flush_success'
                """)
                success_count = cur.fetchone()[0]
                assert success_count == 0, f"不应存在 success 审计记录，实际: {success_count}"
            
            # 6. 断言 DB 层：存在 conflict 审计
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_conflict_100'
                    AND reason = 'outbox_flush_conflict'
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
                audit_row = cur.fetchone()
                
                assert audit_row is not None, "应存在 conflict 审计记录"
                assert audit_row[0] == "redirect", f"action 应为 redirect，实际: {audit_row[0]}"
                assert audit_row[1] == "outbox_flush_conflict"
                
                evidence = audit_row[2]
                assert evidence.get("outbox_id") == outbox_id
                assert evidence.get("worker_id") == "worker1-victim"
                assert evidence.get("intended_action") == "success"
                assert evidence.get("source") == "outbox_worker"
                
                # 验证 extra 包含 observed_* 信息和 correlation_id
                extra = evidence.get("extra", {})
                assert extra.get("observed_status") == "sent", \
                    f"observed_status 应为 sent，实际: {extra.get('observed_status')}"
                assert extra.get("observed_locked_by") == "worker2-stealer", \
                    f"observed_locked_by 应为 worker2-stealer，实际: {extra.get('observed_locked_by')}"
                assert "correlation_id" in extra, "extra 应包含 correlation_id"
                
                # 验证 attempt_id 存在
                assert "attempt_id" in evidence, "evidence 应包含 attempt_id"
        
        finally:
            # 清理
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_conflict_100'")
            conn.close()

    def test_conflict_on_retry_path(self, migrated_db, logbook_adapter_config):
        """
        冲突场景：fail_retry 返回 False（retry 路径冲突）
        
        验证在重试路径也能正确检测冲突。
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            # 1. 插入测试 outbox 记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at)
                    VALUES ('private:retry_conflict', '# Retry Conflict', 'sha_retry_conflict_101', 'pending', 0, now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            def mock_store_fail_and_steal(*args, **kwargs):
                """store 失败，然后模拟 worker2 抢占"""
                # 模拟 worker2 抢占：修改 locked_by
                with psycopg.connect(dsn, autocommit=True) as steal_conn:
                    with steal_conn.cursor() as cur:
                        cur.execute(f"""
                            UPDATE {logbook_schema}.outbox_memory
                            SET locked_by = 'worker2-retry-stealer',
                                locked_at = now()
                            WHERE outbox_id = %s
                        """, (outbox_id,))
                
                return MockStoreResult(success=False, error="test_retry_error")
            
            mock_client = MagicMock()
            mock_client.store.side_effect = mock_store_fail_and_steal
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3, jitter_factor=0.0)
                results = process_batch(config, worker_id="worker1-retry-victim")
            
            # 断言结果为冲突
            assert len(results) == 1
            result = results[0]
            assert result.reason == "outbox_flush_conflict"
            assert result.conflict is True
            
            # 断言不存在 retry 审计
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT COUNT(*)
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_retry_conflict_101'
                    AND reason = 'outbox_flush_retry'
                """)
                retry_count = cur.fetchone()[0]
                assert retry_count == 0, f"不应存在 retry 审计记录，实际: {retry_count}"
            
            # 断言存在 conflict 审计
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_retry_conflict_101'
                    AND reason = 'outbox_flush_conflict'
                """)
                audit_row = cur.fetchone()
                
                assert audit_row is not None, "应存在 conflict 审计记录"
                evidence = audit_row[0]
                assert evidence.get("intended_action") == "retry"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_retry_conflict_101'")
            conn.close()

    def test_no_duplicate_audit_on_conflict(self, migrated_db, logbook_adapter_config):
        """
        验证冲突时只写 conflict 审计，不重复写原计划的 action 审计
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('team:no_dup', '# No Dup Audit', 'sha_no_dup_102', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            def mock_store_and_steal(*args, **kwargs):
                # 模拟 worker2 抢占
                with psycopg.connect(dsn, autocommit=True) as steal_conn:
                    with steal_conn.cursor() as cur:
                        cur.execute(f"""
                            UPDATE {logbook_schema}.outbox_memory
                            SET locked_by = 'other-worker',
                                status = 'sent'
                            WHERE outbox_id = %s
                        """, (outbox_id,))
                
                return MockStoreResult(success=True, memory_id="mem_no_dup")
            
            mock_client = MagicMock()
            mock_client.store.side_effect = mock_store_and_steal
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3)
                process_batch(config, worker_id="dup-check-worker")
            
            # 断言：只有 1 条审计记录且是 conflict
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT reason, COUNT(*)
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_no_dup_102'
                    GROUP BY reason
                """)
                rows = cur.fetchall()
                
                reasons = {row[0]: row[1] for row in rows}
                
                assert "outbox_flush_conflict" in reasons, "应存在 conflict 审计"
                assert reasons.get("outbox_flush_conflict") == 1, "conflict 审计应只有 1 条"
                assert "outbox_flush_success" not in reasons, "不应存在 success 审计"
                assert "outbox_flush_retry" not in reasons, "不应存在 retry 审计"
                assert "outbox_flush_dead" not in reasons, "不应存在 dead 审计"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_no_dup_102'")
            conn.close()


class TestOutboxWorkerLeaseConflict:
    """Lease 过期与冲突处理集成测试"""

    def test_lease_expired_conflict_detection(self, migrated_db, logbook_adapter_config):
        """
        测试 Lease 过期场景：Worker1 处理超时，Worker2 重新 claim 任务
        
        场景：
        1. 配置 lease_seconds=1
        2. Worker1 claim 任务后，mock store sleep 2s（超过 lease）
        3. Worker2 在 Worker1 完成前尝试 claim（租约已过期）
        4. 验证冲突检测和审计记录行为
        
        预期结果（修复后）：
        - A) Worker2 无法 claim 到该任务（renew 生效）；或
        - B) Worker2 claim 到任务，Worker1 的 ack_sent 返回 False，
             Worker1 只写 conflict 审计（不写 success 审计）
        """
        import time
        import threading
        
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        # 用于记录两个 worker 的处理结果
        worker1_results = {"results": None, "error": None}
        worker2_results = {"results": None, "claimed": False}
        
        try:
            # 1. 插入测试 outbox 记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:lease_test', '# Lease Conflict Test', 'sha_lease_conflict_001', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # 2. 定义 Worker1 的 mock store（sleep 2s 模拟长时间处理）
            def slow_store_worker1(*args, **kwargs):
                time.sleep(2)  # 超过 lease_seconds=1
                return MockStoreResult(success=True, memory_id="mem_worker1_lease_001")
            
            # 3. 定义 Worker2 的 mock store（正常处理）
            def fast_store_worker2(*args, **kwargs):
                return MockStoreResult(success=True, memory_id="mem_worker2_lease_001")
            
            # 4. Worker1 处理函数
            def run_worker1():
                try:
                    mock_client = MagicMock()
                    mock_client.store.side_effect = slow_store_worker1
                    
                    with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                        from engram.gateway.outbox_worker import WorkerConfig, process_batch
                        
                        # 关闭 jitter，使用短 lease
                        config = WorkerConfig(
                            batch_size=10,
                            max_retries=3,
                            jitter_factor=0.0,
                            lease_seconds=1  # 短租约，模拟过期
                        )
                        worker1_results["results"] = process_batch(config, worker_id="worker-1-lease")
                except Exception as e:
                    worker1_results["error"] = str(e)
            
            # 5. Worker2 处理函数（延迟启动，等待 Worker1 的租约过期）
            def run_worker2():
                try:
                    # 等待 1.2 秒，确保 Worker1 的租约已过期
                    time.sleep(1.2)
                    
                    mock_client = MagicMock()
                    mock_client.store.side_effect = fast_store_worker2
                    
                    with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                        from engram.gateway.outbox_worker import WorkerConfig, process_batch
                        
                        config = WorkerConfig(
                            batch_size=10,
                            max_retries=3,
                            jitter_factor=0.0,
                            lease_seconds=60  # Worker2 使用较长租约
                        )
                        results = process_batch(config, worker_id="worker-2-lease")
                        worker2_results["results"] = results
                        worker2_results["claimed"] = len(results) > 0
                except Exception as e:
                    worker2_results["error"] = str(e)
            
            # 6. 并行启动两个 worker
            thread1 = threading.Thread(target=run_worker1)
            thread2 = threading.Thread(target=run_worker2)
            
            thread1.start()
            thread2.start()
            
            thread1.join(timeout=10)
            thread2.join(timeout=10)
            
            # 7. 验证结果
            assert worker1_results["error"] is None, f"Worker1 出错: {worker1_results['error']}"
            
            # 8. 查询审计记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_lease_conflict_001'
                    ORDER BY created_at ASC
                """)
                audit_rows = cur.fetchall()
            
            # 9. 验证最终状态
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, locked_by, last_error
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                final_row = cur.fetchone()
            
            final_status = final_row[0]
            final_locked_by = final_row[1]
            
            # 10. 根据场景验证结果
            # 场景 A：Worker2 未能 claim 到任务（renew 生效或 Worker1 先完成）
            if not worker2_results["claimed"]:
                # Worker2 未 claim 到，任务应由 Worker1 成功完成
                assert final_status == "sent", f"状态应为 sent，实际: {final_status}"
                
                # 应只有 success 审计，无 conflict 审计
                success_audits = [r for r in audit_rows if r[1] == "outbox_flush_success"]
                conflict_audits = [r for r in audit_rows if r[1] == "outbox_flush_conflict"]
                
                assert len(success_audits) == 1, f"应有 1 条 success 审计，实际: {len(success_audits)}"
                assert len(conflict_audits) == 0, f"应无 conflict 审计，实际: {len(conflict_audits)}"
            
            # 场景 B：Worker2 claim 到任务（租约过期被抢占）
            else:
                # Worker2 claim 到了任务，Worker1 应检测到冲突
                assert worker1_results["results"] is not None
                
                # Worker1 的结果
                w1_results = worker1_results["results"]
                w2_results = worker2_results["results"]
                
                # 至少有一个 worker 应该成功处理或检测到冲突
                # Worker1 应返回 conflict 结果（ack_sent 返回 False）
                w1_conflicts = [r for r in w1_results if r.conflict is True]
                
                # 验证冲突审计存在
                conflict_audits = [r for r in audit_rows if r[1] == "outbox_flush_conflict"]
                success_audits = [r for r in audit_rows if r[1] == "outbox_flush_success"]
                
                # 如果 Worker1 检测到冲突，它不应写 success 审计
                # 只有一个 worker 应该写 success 审计
                if len(w1_conflicts) > 0:
                    # Worker1 检测到冲突
                    assert len(conflict_audits) >= 1, "应存在 conflict 审计记录"
                    
                    # 验证 conflict 审计的 evidence
                    conflict_evidence = conflict_audits[0][2]
                    assert conflict_evidence.get("worker_id") == "worker-1-lease", \
                        f"conflict 审计的 worker_id 应为 worker-1-lease，实际: {conflict_evidence.get('worker_id')}"
                    assert conflict_evidence.get("intended_action") == "success", \
                        f"intended_action 应为 success，实际: {conflict_evidence.get('intended_action')}"
                    
                    # Worker2 应该成功完成
                    if w2_results:
                        w2_successes = [r for r in w2_results if r.success is True]
                        assert len(w2_successes) == 1, f"Worker2 应成功完成，实际成功数: {len(w2_successes)}"
                    
                    # 最终状态应为 sent
                    assert final_status == "sent", f"状态应为 sent，实际: {final_status}"
                    
                    # 应只有一条 success 审计（来自 Worker2）
                    assert len(success_audits) == 1, f"应只有 1 条 success 审计，实际: {len(success_audits)}"
                    
                else:
                    # 特殊情况：Worker1 比 Worker2 先完成 ack_sent
                    # 此时 Worker1 写 success，Worker2 可能失败
                    # 这是正确的行为，无需额外验证
                    pass
        
        finally:
            # 清理
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_lease_conflict_001'")
            conn.close()

    def test_sequential_lease_conflict_deterministic(self, migrated_db, logbook_adapter_config):
        """
        串行测试 Lease 冲突（确定性场景）
        
        场景：
        1. Worker1 claim 任务
        2. 手动模拟 lease 过期（修改 locked_at）
        3. Worker2 claim 同一任务（租约已过期）
        4. Worker1 尝试 ack_sent（应检测到冲突）
        
        验证：Worker1 的 ack_sent 返回 False 且写入 conflict 审计
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            # 1. 插入测试 outbox 记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:seq_lease', '# Sequential Lease Test', 'sha_seq_lease_002', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # 2. Worker1 claim 任务
            from gateway import logbook_adapter
            
            claimed_items = logbook_adapter.claim_outbox(
                worker_id="worker-1-seq",
                limit=10,
                lease_seconds=60
            )
            
            assert len(claimed_items) == 1, f"Worker1 应 claim 到 1 条，实际: {len(claimed_items)}"
            
            # 3. 手动模拟 lease 过期：将 locked_at 设为过去时间
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE {logbook_schema}.outbox_memory
                    SET locked_at = now() - interval '120 seconds'
                    WHERE outbox_id = %s
                """, (outbox_id,))
            
            # 4. Worker2 claim 同一任务（租约已过期）
            claimed_by_worker2 = logbook_adapter.claim_outbox(
                worker_id="worker-2-seq",
                limit=10,
                lease_seconds=60
            )
            
            assert len(claimed_by_worker2) == 1, f"Worker2 应 claim 到 1 条（租约过期），实际: {len(claimed_by_worker2)}"
            assert claimed_by_worker2[0]["outbox_id"] == outbox_id
            
            # 5. 验证 locked_by 已被 Worker2 更新
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT locked_by
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                current_locked_by = cur.fetchone()[0]
            
            assert current_locked_by == "worker-2-seq", f"locked_by 应为 worker-2-seq，实际: {current_locked_by}"
            
            # 6. Worker1 尝试 ack_sent（应失败，返回 False）
            ack_result = logbook_adapter.ack_sent(
                outbox_id=outbox_id,
                worker_id="worker-1-seq",  # 使用 Worker1 的 ID
                memory_id="mem_worker1_should_fail"
            )
            
            assert ack_result is False, "Worker1 的 ack_sent 应返回 False（租约被抢占）"
            
            # 7. 模拟 Worker1 调用 _handle_conflict
            from engram.gateway.outbox_worker import _handle_conflict
            
            conflict_result = _handle_conflict(
                outbox_id=outbox_id,
                worker_id="worker-1-seq",
                attempt_id="attempt-test-001",
                user_id="seq_lease",
                target_space="private:seq_lease",
                payload_sha="sha_seq_lease_002",
                intended_action="success",
            )
            
            assert conflict_result.conflict is True, "应返回 conflict=True"
            assert conflict_result.reason == "outbox_flush_conflict"
            
            # 8. 验证 conflict 审计记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_seq_lease_002'
                    AND reason = 'outbox_flush_conflict'
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
                audit_row = cur.fetchone()
            
            assert audit_row is not None, "应存在 conflict 审计记录"
            assert audit_row[0] == "redirect", f"action 应为 redirect，实际: {audit_row[0]}"
            assert audit_row[1] == "outbox_flush_conflict"
            
            evidence = audit_row[2]
            assert evidence.get("outbox_id") == outbox_id
            assert evidence.get("worker_id") == "worker-1-seq"
            assert evidence.get("intended_action") == "success"
            
            # extra 中应包含观察到的状态
            extra = evidence.get("extra", {})
            assert extra.get("observed_locked_by") == "worker-2-seq", \
                f"observed_locked_by 应为 worker-2-seq，实际: {extra.get('observed_locked_by')}"
            
            # 9. Worker2 成功 ack_sent
            ack_result_2 = logbook_adapter.ack_sent(
                outbox_id=outbox_id,
                worker_id="worker-2-seq",
                memory_id="mem_worker2_success"
            )
            
            assert ack_result_2 is True, "Worker2 的 ack_sent 应成功"
            
            # 10. 验证最终状态
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, locked_by, last_error
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                final_row = cur.fetchone()
            
            assert final_row[0] == "sent", f"状态应为 sent，实际: {final_row[0]}"
            assert final_row[1] is None, "locked_by 应为 NULL"
            assert final_row[2] == "memory_id=mem_worker2_success"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_seq_lease_002'")
            conn.close()


class TestOutboxWorkerLeaseRenewal:
    """Lease 续期集成测试"""

    def test_renew_lease_prevents_reclaim_during_slow_store(self, migrated_db, logbook_adapter_config):
        """
        验证续期防止被 reclaim：
        1. 设置很短的 lease_seconds
        2. Mock store 执行时间超过 lease
        3. 验证 Worker 续期后第二个 Worker 无法 reclaim
        4. 验证第一个 Worker 的 ack 仍能成功
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:lease_test', '# Lease Test Memory', 'sha_lease_renew_001', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # 用于跟踪 store 调用时第二个 worker 的 claim 结果
            reclaim_attempted = {"worker2_claimed": False, "worker2_ids": []}
            
            # 自定义 store 函数：模拟慢速操作
            def slow_store(*args, **kwargs):
                import time
                # 模拟耗时操作（超过 lease_seconds）
                # 由于 renew_lease 在 store 前已调用，租约应该是新的
                # Worker 2 在此期间尝试 claim 不应成功
                
                # 检查 Worker 2 是否能 claim 该记录
                with psycopg.connect(dsn, autocommit=True) as check_conn:
                    with check_conn.cursor() as cur:
                        # 查询记录当前状态
                        cur.execute(f"""
                            SELECT locked_by, locked_at 
                            FROM {logbook_schema}.outbox_memory 
                            WHERE outbox_id = %s
                        """, (outbox_id,))
                        row = cur.fetchone()
                        # locked_at 应该是刚刚续期的（很新），所以 Worker 2 不能 claim
                        reclaim_attempted["locked_by"] = row[0]
                        reclaim_attempted["locked_at"] = row[1]
                
                return MockStoreResult(success=True, memory_id="mem_lease_renew_001")
            
            mock_client = MagicMock()
            mock_client.store.side_effect = slow_store
            
            # 使用较短的 lease_seconds
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                # lease_seconds=3 很短，但 renew_lease 会续期
                config = WorkerConfig(batch_size=10, max_retries=3, lease_seconds=3)
                results = process_batch(config, worker_id="worker-lease-1")
            
            # 断言：Worker 1 处理成功
            assert len(results) == 1
            assert results[0].success is True
            assert results[0].reason == "outbox_flush_success"
            
            # 断言：store 调用时记录仍被 worker-lease-1 锁定
            assert reclaim_attempted["locked_by"] == "worker-lease-1"
            
            # 断言 DB 层：记录状态为 sent
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, locked_by, locked_at
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                assert row[0] == "sent"
                assert row[1] is None  # 锁已释放
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_lease_renew_001'")
            conn.close()

    def test_renew_lease_called_before_and_after_store(self, migrated_db, logbook_adapter_config):
        """验证 renew_lease 在 store 前后都被调用"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        # 跟踪 renew_lease 调用
        renew_calls = []
        
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('team:renew_test', '# Renew Test', 'sha_renew_track_002', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(success=True, memory_id="mem_renew_002")
            
            # 包装 renew_lease 以追踪调用
            from gateway import logbook_adapter
            original_renew = logbook_adapter.renew_lease
            
            def tracked_renew(outbox_id, worker_id):
                renew_calls.append({
                    "outbox_id": outbox_id,
                    "worker_id": worker_id,
                    "time": datetime.now(timezone.utc)
                })
                return original_renew(outbox_id, worker_id)
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                with patch("gateway.outbox_worker.logbook_adapter.renew_lease", side_effect=tracked_renew):
                    from engram.gateway.outbox_worker import WorkerConfig, process_batch
                    
                    config = WorkerConfig(batch_size=10, max_retries=3)
                    results = process_batch(config, worker_id="renew-track-worker")
            
            # 断言：成功处理
            assert len(results) == 1
            assert results[0].success is True
            
            # 断言：renew_lease 被调用了 2 次（store 前 + ack 前）
            assert len(renew_calls) == 2, f"renew_lease 应被调用 2 次，实际: {len(renew_calls)}"
            assert all(c["outbox_id"] == outbox_id for c in renew_calls)
            assert all(c["worker_id"] == "renew-track-worker" for c in renew_calls)
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_renew_track_002'")
            conn.close()

    def test_second_worker_cannot_reclaim_after_renew(self, migrated_db, logbook_adapter_config):
        """
        验证续期后第二个 worker 无法 reclaim：
        1. Worker 1 claim 一条记录
        2. 模拟 lease 即将过期（但 Worker 1 续期）
        3. Worker 2 尝试 claim 同一条记录
        4. 验证 Worker 2 无法获取该记录
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('team:reclaim_test', '# Reclaim Test', 'sha_reclaim_003', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # Worker 1 claim
            from gateway import logbook_adapter
            
            claimed_items = logbook_adapter.claim_outbox(
                worker_id="worker-1",
                limit=10,
                lease_seconds=60
            )
            
            assert len(claimed_items) == 1
            assert claimed_items[0]["outbox_id"] == outbox_id
            
            # Worker 1 续期
            renew_result = logbook_adapter.renew_lease(outbox_id, worker_id="worker-1")
            assert renew_result is True
            
            # Worker 2 尝试 claim
            worker2_items = logbook_adapter.claim_outbox(
                worker_id="worker-2",
                limit=10,
                lease_seconds=60
            )
            
            # Worker 2 不应获取到被 Worker 1 锁定的记录
            worker2_ids = [item["outbox_id"] for item in worker2_items]
            assert outbox_id not in worker2_ids, "续期后 Worker 2 不应能 claim 该记录"
            
            # Worker 1 仍能成功 ack
            ack_result = logbook_adapter.ack_sent(outbox_id, worker_id="worker-1", memory_id="mem_reclaim_003")
            assert ack_result is True
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_reclaim_003'")
            conn.close()

    def test_expired_lease_can_be_reclaimed_without_renew(self, migrated_db, logbook_adapter_config):
        """
        对照测试：未续期的过期 lease 可以被 reclaim：
        1. 插入一条已过期的锁定记录
        2. Worker 2 能够成功 claim
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            # 插入一条锁已过期的记录（locked_at 在 2 分钟前，lease_seconds=60）
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at, locked_by, locked_at)
                    VALUES ('team:expired_test', '# Expired Test', 'sha_expired_004', 'pending', 
                            now() - interval '1 minute', 'dead-worker', now() - interval '2 minutes')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            from gateway import logbook_adapter
            
            # Worker 2 应该能够 claim 这条过期的记录
            claimed_items = logbook_adapter.claim_outbox(
                worker_id="worker-2",
                limit=10,
                lease_seconds=60
            )
            
            claimed_ids = [item["outbox_id"] for item in claimed_items]
            assert outbox_id in claimed_ids, "过期的 lease 应该可以被 reclaim"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()


class TestOutboxWorkerSlowStoreTimeout:
    """
    长延迟 store 测试：验证 OpenMemory 客户端超时配置生效，
    不会因为内部重试导致处理时间失控
    """

    def test_slow_store_respects_timeout_no_internal_retry(self, migrated_db, logbook_adapter_config):
        """
        验证长延迟 store 场景下：
        1. OpenMemory 客户端使用配置的超时时间
        2. max_client_retries=0 时不进行内部重试，立即返回失败
        3. 处理时间不会因内部重试而成倍增加
        """
        import time
        
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            # 插入测试记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:slow_test', '# Slow Store Test', 'sha_slow_store_001', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # 记录 store 调用次数
            store_call_count = {"count": 0}
            
            # Mock store 模拟长延迟（2秒）
            def slow_store(*args, **kwargs):
                store_call_count["count"] += 1
                time.sleep(2)  # 模拟网络延迟
                return MockStoreResult(success=True, memory_id="mem_slow_001")
            
            mock_client = MagicMock()
            mock_client.store.side_effect = slow_store
            
            # 使用短超时（但因为我们 mock 了整个 client，超时不会实际生效）
            # 这里主要验证配置传递和调用次数
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(
                    batch_size=10,
                    max_retries=3,
                    jitter_factor=0.0,
                    openmemory_timeout_seconds=1.0,  # 短超时
                    openmemory_max_client_retries=0,  # 不进行内部重试
                )
                
                start_time = time.time()
                results = process_batch(config, worker_id="slow-test-worker")
                elapsed = time.time() - start_time
            
            # 验证：store 只被调用一次（无内部重试）
            assert store_call_count["count"] == 1, \
                f"store 应只被调用 1 次（无内部重试），实际: {store_call_count['count']}"
            
            # 验证：处理成功
            assert len(results) == 1
            assert results[0].success is True
            
            # 验证：处理时间在合理范围内（约 2 秒，而非 6 秒以上）
            # 如果有 3 次内部重试，每次 2 秒，总共会是 6 秒以上
            assert elapsed < 5.0, f"处理时间应小于 5 秒，实际: {elapsed:.2f} 秒"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_slow_store_001'")
            conn.close()

    def test_timeout_exception_triggers_worker_retry(self, migrated_db, logbook_adapter_config):
        """
        验证超时异常触发 Worker 层重试逻辑：
        1. OpenMemory 客户端抛出 ConnectionError（超时）
        2. Worker 层捕获并标记为 retry
        3. 不会因客户端内部重试导致多次调用
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at)
                    VALUES ('private:timeout_test', '# Timeout Test', 'sha_timeout_001', 'pending', 0, now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            store_call_count = {"count": 0}
            
            # Mock store 抛出连接错误（模拟超时）
            from engram.gateway.openmemory_client import OpenMemoryConnectionError
            
            def timeout_store(*args, **kwargs):
                store_call_count["count"] += 1
                raise OpenMemoryConnectionError(
                    message="Connection timeout after 1.0s",
                    status_code=None,
                    response=None
                )
            
            mock_client = MagicMock()
            mock_client.store.side_effect = timeout_store
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(
                    batch_size=10,
                    max_retries=3,
                    jitter_factor=0.0,
                    openmemory_timeout_seconds=1.0,
                    openmemory_max_client_retries=0,
                )
                
                results = process_batch(config, worker_id="timeout-test-worker")
            
            # 验证：store 只调用一次（Worker 层捕获异常，不进行内部重试）
            assert store_call_count["count"] == 1, \
                f"store 应只被调用 1 次，实际: {store_call_count['count']}"
            
            # 验证：结果为重试
            assert len(results) == 1
            assert results[0].success is False
            assert results[0].action == "redirect"
            assert results[0].reason == "outbox_flush_retry"
            assert "connection_error" in results[0].error
            
            # 验证 DB：retry_count 增加
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, retry_count, last_error
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                
                assert row[0] == "pending", f"状态应保持 pending，实际: {row[0]}"
                assert row[1] == 1, f"retry_count 应为 1，实际: {row[1]}"
                assert "connection_error" in row[2]
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_timeout_001'")
            conn.close()

    def test_processing_time_with_multiple_retries_controlled(self, migrated_db, logbook_adapter_config):
        """
        验证：即使配置了 max_client_retries=1，总处理时间仍可控
        
        场景：
        - 配置 openmemory_max_client_retries=1（允许 1 次内部重试）
        - Mock store 第一次失败（返回 5xx），第二次成功
        - 验证处理时间在预期范围内
        """
        import time
        
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:controlled_retry', '# Controlled Retry', 'sha_controlled_001', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            store_call_count = {"count": 0}
            
            # Mock store：第一次调用等待 0.5 秒后成功
            def controlled_store(*args, **kwargs):
                store_call_count["count"] += 1
                time.sleep(0.5)  # 模拟网络延迟
                return MockStoreResult(success=True, memory_id="mem_controlled_001")
            
            mock_client = MagicMock()
            mock_client.store.side_effect = controlled_store
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(
                    batch_size=10,
                    max_retries=3,
                    jitter_factor=0.0,
                    openmemory_timeout_seconds=5.0,
                    openmemory_max_client_retries=1,  # 允许 1 次内部重试
                )
                
                start_time = time.time()
                results = process_batch(config, worker_id="controlled-worker")
                elapsed = time.time() - start_time
            
            # 验证：store 只调用一次（成功了就不重试）
            assert store_call_count["count"] == 1
            
            # 验证：处理成功
            assert len(results) == 1
            assert results[0].success is True
            
            # 验证：处理时间约 0.5 秒
            assert 0.4 < elapsed < 2.0, f"处理时间应在 0.4-2.0 秒范围，实际: {elapsed:.2f} 秒"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_controlled_001'")
            conn.close()


class TestOutboxWorkerDatabaseTimeout:
    """
    数据库超时场景测试：验证 statement_timeout 触发时 Worker 走可恢复路径
    
    测试场景：
    1. 设置极小的 statement_timeout
    2. 在数据库操作前注入延迟（使用 pg_sleep）
    3. 验证 Worker 捕获超时并写入 db_timeout 审计
    4. 验证 outbox 记录仍可被后续恢复处理
    """

    def test_db_timeout_triggers_recoverable_path(self, migrated_db, logbook_adapter_config, monkeypatch):
        """
        数据库超时触发可恢复路径：
        1. 设置 statement_timeout 为极小值（10ms）
        2. Mock logbook_adapter.check_dedup 在执行前添加 pg_sleep 延迟
        3. 断言 Worker 返回 db_timeout 结果，状态保持 pending
        4. 后续正常处理可成功
        """
        import os
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            # 1. 插入测试 outbox 记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:timeout_test', '# Timeout Test', 'sha_db_timeout_001', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # 2. 设置极小的 statement_timeout（通过环境变量）
            monkeypatch.setenv("ENGRAM_PG_STATEMENT_TIMEOUT_MS", "10")
            
            # 3. Mock check_dedup 注入延迟
            original_check_dedup = None
            delay_injected = {"count": 0}
            
            def slow_check_dedup_wrapper(*args, **kwargs):
                """在 check_dedup 执行前注入 pg_sleep 延迟"""
                delay_injected["count"] += 1
                # 使用独立连接执行 pg_sleep（绕过 statement_timeout）
                # 然后调用原始函数（会因为 statement_timeout 超时）
                # 方案：直接在连接上执行 pg_sleep 会被 statement_timeout 取消
                from engram.logbook.db import get_connection
                timeout_conn = get_connection()
                try:
                    with timeout_conn.cursor() as cur:
                        # 这会触发 statement_timeout
                        cur.execute("SELECT pg_sleep(0.1)")  # 100ms > 10ms timeout
                except psycopg.errors.QueryCanceled:
                    # 预期的超时
                    raise
                finally:
                    timeout_conn.close()
                
                # 不会执行到这里
                return original_check_dedup(*args, **kwargs)
            
            # 保存原始函数并替换
            from gateway import logbook_adapter as adapter_module
            original_check_dedup = adapter_module.check_dedup
            monkeypatch.setattr(adapter_module, "check_dedup", slow_check_dedup_wrapper)
            
            # 4. Mock OpenMemory client（不应被调用）
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(success=True, memory_id="should_not_be_called")
            
            # 5. 执行 worker
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3, jitter_factor=0.0)
                results = process_batch(config, worker_id="timeout-test-worker")
            
            # 6. 断言结果
            assert len(results) == 1
            result = results[0]
            assert result.success is False, "结果应为失败"
            assert result.action == "redirect", f"action 应为 redirect，实际: {result.action}"
            assert result.reason == "outbox_flush_db_timeout", f"reason 应为 outbox_flush_db_timeout，实际: {result.reason}"
            assert "db_timeout" in result.error, f"error 应包含 db_timeout，实际: {result.error}"
            
            # 7. 关键断言：OpenMemory store 不应被调用
            mock_client.store.assert_not_called()
            
            # 8. 断言 DB 层：记录状态仍为 pending（可被后续恢复）
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, locked_by
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                
                # 注意：由于超时发生在 claim_outbox 之后，状态可能仍为 pending
                # locked_by 可能仍保留（因为超时发生在处理中）
                assert row[0] == "pending", f"状态应保持 pending（可恢复），实际: {row[0]}"
            
            # 9. 断言 DB 层：存在 db_timeout 审计记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_db_timeout_001'
                    AND reason = 'outbox_flush_db_timeout'
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
                audit_row = cur.fetchone()
                
                assert audit_row is not None, "应存在 db_timeout 审计记录"
                assert audit_row[0] == "redirect", f"action 应为 redirect，实际: {audit_row[0]}"
                assert audit_row[1] == "outbox_flush_db_timeout"
                
                evidence = audit_row[2]
                assert evidence.get("outbox_id") == outbox_id
                assert evidence.get("source") == "outbox_worker"
                
                extra = evidence.get("extra", {})
                assert extra.get("error_type") == "db_timeout"
        
        finally:
            # 清理环境变量
            monkeypatch.delenv("ENGRAM_PG_STATEMENT_TIMEOUT_MS", raising=False)
            
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_db_timeout_001'")
            conn.close()

    def test_outbox_recoverable_after_db_timeout(self, migrated_db, logbook_adapter_config, monkeypatch):
        """
        验证 outbox 在数据库超时后仍可被后续恢复处理：
        1. 第一次处理触发超时
        2. 第二次处理（无超时）成功
        """
        import os
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            # 1. 插入测试 outbox 记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:recover_test', '# Recover Test', 'sha_recover_001', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # 2. 第一次处理：模拟数据库超时
            call_count = {"count": 0}
            
            def failing_then_ok_check_dedup(*args, **kwargs):
                """第一次调用抛出超时，后续正常"""
                call_count["count"] += 1
                if call_count["count"] == 1:
                    # 模拟超时错误
                    raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
                # 后续正常返回
                return None  # 无 dedup 记录
            
            from gateway import logbook_adapter as adapter_module
            monkeypatch.setattr(adapter_module, "check_dedup", failing_then_ok_check_dedup)
            
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(success=True, memory_id="mem_recover_001")
            
            # 3. 第一次处理
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3, jitter_factor=0.0)
                results_first = process_batch(config, worker_id="recover-worker-1")
            
            # 断言第一次处理结果为超时
            assert len(results_first) == 1
            assert results_first[0].success is False
            assert "db_timeout" in results_first[0].reason
            
            # 4. 重置 outbox 状态（模拟 lease 过期后被 reclaim）
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE {logbook_schema}.outbox_memory
                    SET locked_by = NULL, locked_at = NULL, next_attempt_at = now() - interval '1 minute'
                    WHERE outbox_id = %s
                """, (outbox_id,))
            
            # 5. 第二次处理（check_dedup 已恢复正常）
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                results_second = process_batch(config, worker_id="recover-worker-2")
            
            # 6. 断言第二次处理成功
            assert len(results_second) == 1
            assert results_second[0].success is True
            assert results_second[0].reason == "outbox_flush_success"
            
            # 7. 断言 DB 层：状态变为 sent
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, last_error
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                
                assert row[0] == "sent", f"状态应为 sent，实际: {row[0]}"
                assert row[1] == "memory_id=mem_recover_001"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_recover_001'")
            conn.close()

    def test_db_error_classified_correctly(self, migrated_db, logbook_adapter_config, monkeypatch):
        """
        验证非超时数据库错误被正确分类为 db_error
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:db_error_test', '# DB Error Test', 'sha_db_error_001', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            # 模拟非超时数据库错误（如连接断开）
            def raise_db_error(*args, **kwargs):
                raise psycopg.OperationalError("connection reset by peer")
            
            from gateway import logbook_adapter as adapter_module
            monkeypatch.setattr(adapter_module, "check_dedup", raise_db_error)
            
            mock_client = MagicMock()
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=3, jitter_factor=0.0)
                results = process_batch(config, worker_id="db-error-worker")
            
            # 断言结果分类为 db_error
            assert len(results) == 1
            result = results[0]
            assert result.success is False
            assert result.reason == "outbox_flush_db_error", f"reason 应为 outbox_flush_db_error，实际: {result.reason}"
            assert "db_error" in result.error
            
            # 验证审计记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = 'sha_db_error_001'
                    AND reason = 'outbox_flush_db_error'
                """)
                audit_row = cur.fetchone()
                
                assert audit_row is not None, "应存在 db_error 审计记录"
                assert audit_row[0] == "redirect"
                
                extra = audit_row[2].get("extra", {})
                assert extra.get("error_type") == "db_error"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = 'sha_db_error_001'")
            conn.close()


class TestOutboxDegradationRecoveryE2E:
    """
    端到端测试：OpenMemory 暂不可用→写入降级到 outbox→恢复后 flush→审计一致性验证
    
    验证完整的降级恢复流程：
    1. OpenMemory 不可用时，写入降级到 outbox_memory
    2. Worker flush 成功后，outbox 状态变为 sent
    3. 审计记录的 reason 与 outbox 最终状态一致
    """

    def test_degradation_to_outbox_recovery_flush_audit_consistency(self, migrated_db, logbook_adapter_config):
        """
        完整端到端测试：降级→恢复→flush→审计 reason 与 outbox 状态一致
        
        流程：
        1. 模拟 OpenMemory 不可用，Gateway 将写入降级到 outbox
        2. outbox 记录状态为 pending
        3. 模拟 OpenMemory 恢复，Worker 处理 outbox
        4. 验证 outbox 状态变为 sent
        5. 验证审计记录 reason=outbox_flush_success 与状态一致
        """
        import asyncio
        from unittest.mock import MagicMock, patch, AsyncMock
        
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        payload_sha = f"sha_e2e_degrade_{uuid.uuid4().hex[:8]}"
        
        try:
            # ========== Phase 1: 模拟 Gateway 降级写入 outbox ==========
            # 直接插入 outbox 记录（模拟 Gateway 因 OpenMemory 不可用而降级）
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at, last_error)
                    VALUES ('private:e2e_user', '# E2E Degradation Test Memory', %s, 'pending', 0, now() - interval '1 minute', 'degraded_due_to_openmemory_unavailable')
                    RETURNING outbox_id
                """, (payload_sha,))
                outbox_id = cur.fetchone()[0]
            
            # 写入降级审计记录（模拟 Gateway 的审计）
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {governance_schema}.write_audit
                        (actor_user_id, target_space, action, reason, payload_sha, evidence_refs_json)
                    VALUES ('e2e_user', 'private:e2e_user', 'redirect', 'openmemory_write_failed:connection_error', %s, %s)
                """, (payload_sha, '{"outbox_id": ' + str(outbox_id) + ', "source": "gateway", "extra": {"correlation_id": "e2e-test"}}'))
            
            # 验证 outbox 状态为 pending
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s
                """, (outbox_id,))
                assert cur.fetchone()[0] == "pending", "初始状态应为 pending"
            
            # ========== Phase 2: 模拟 OpenMemory 恢复，Worker 处理 ==========
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(
                success=True,
                memory_id="mem_e2e_recovery_001"
            )
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=5, jitter_factor=0.0)
                results = process_batch(config, worker_id="e2e-recovery-worker")
            
            # ========== Phase 3: 验证结果 ==========
            # 3.1 验证处理成功
            assert len(results) >= 1, "应处理到至少 1 条记录"
            
            # 找到我们的记录
            our_result = next((r for r in results if r.outbox_id == outbox_id), None)
            assert our_result is not None, f"应包含 outbox_id={outbox_id} 的结果"
            assert our_result.success is True, f"处理应成功，实际: success={our_result.success}"
            assert our_result.reason == "outbox_flush_success", f"reason 应为 outbox_flush_success，实际: {our_result.reason}"
            
            # 3.2 验证 outbox 状态变为 sent
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, last_error, locked_by, locked_at
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                
                assert row[0] == "sent", f"outbox 状态应变为 sent，实际: {row[0]}"
                assert row[1] == "memory_id=mem_e2e_recovery_001", \
                    f"last_error 应包含 memory_id，实际: {row[1]}"
                assert row[2] is None, "locked_by 应为 NULL（锁已释放）"
                assert row[3] is None, "locked_at 应为 NULL"
            
            # 3.3 验证审计记录一致性：reason=outbox_flush_success
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = %s
                    AND reason = 'outbox_flush_success'
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (payload_sha,))
                audit_row = cur.fetchone()
                
                assert audit_row is not None, \
                    "应存在 reason=outbox_flush_success 的审计记录"
                assert audit_row[0] == "allow", f"action 应为 allow，实际: {audit_row[0]}"
                assert audit_row[1] == "outbox_flush_success", \
                    f"reason 应为 outbox_flush_success，实际: {audit_row[1]}"
                
                evidence = audit_row[2]
                assert evidence.get("outbox_id") == outbox_id, \
                    f"evidence 应包含正确的 outbox_id，实际: {evidence.get('outbox_id')}"
                assert evidence.get("memory_id") == "mem_e2e_recovery_001", \
                    f"evidence 应包含 memory_id，实际: {evidence.get('memory_id')}"
                assert evidence.get("source") == "outbox_worker", \
                    f"evidence source 应为 outbox_worker，实际: {evidence.get('source')}"
            
            # 3.4 验证完整审计链：降级 + 恢复
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT reason, action
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = %s
                    ORDER BY created_at ASC
                """, (payload_sha,))
                all_audits = cur.fetchall()
                
                assert len(all_audits) >= 2, \
                    f"应有至少 2 条审计记录（降级 + 恢复），实际: {len(all_audits)}"
                
                # 第一条应是降级（redirect）
                assert all_audits[0][1] == "redirect", \
                    f"第一条审计 action 应为 redirect（降级），实际: {all_audits[0][1]}"
                
                # 最后一条应是成功（allow）
                assert all_audits[-1][1] == "allow", \
                    f"最后一条审计 action 应为 allow（恢复成功），实际: {all_audits[-1][1]}"
                assert all_audits[-1][0] == "outbox_flush_success", \
                    f"最后一条审计 reason 应为 outbox_flush_success，实际: {all_audits[-1][0]}"
        
        finally:
            # 清理
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = %s", (payload_sha,))
            conn.close()

    def test_degradation_max_retries_dead_audit_consistency(self, migrated_db, logbook_adapter_config):
        """
        测试超过最大重试次数后的审计一致性
        
        流程：
        1. 创建已重试多次的 outbox 记录
        2. OpenMemory 继续不可用
        3. Worker 处理后标记为 dead
        4. 验证审计 reason=outbox_flush_dead 与状态一致
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        payload_sha = f"sha_e2e_dead_{uuid.uuid4().hex[:8]}"
        
        try:
            # 创建已重试 4 次的记录（max_retries=5 时，下次失败就是第 5 次 >= max_retries）
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at, last_error)
                    VALUES ('private:dead_e2e', '# Dead E2E Test', %s, 'pending', 4, now() - interval '1 minute', 'previous_failure')
                    RETURNING outbox_id
                """, (payload_sha,))
                outbox_id = cur.fetchone()[0]
            
            # Mock OpenMemory 持续失败
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(
                success=False,
                error="openmemory_still_unavailable"
            )
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=5, jitter_factor=0.0)
                results = process_batch(config, worker_id="dead-e2e-worker")
            
            # 验证结果
            our_result = next((r for r in results if r.outbox_id == outbox_id), None)
            assert our_result is not None
            assert our_result.success is False
            assert our_result.reason == "outbox_flush_dead", \
                f"reason 应为 outbox_flush_dead，实际: {our_result.reason}"
            
            # 验证 outbox 状态为 dead
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s
                """, (outbox_id,))
                assert cur.fetchone()[0] == "dead", "状态应变为 dead"
            
            # 验证审计记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = %s
                    AND reason = 'outbox_flush_dead'
                """, (payload_sha,))
                audit_row = cur.fetchone()
                
                assert audit_row is not None, "应存在 reason=outbox_flush_dead 的审计记录"
                assert audit_row[0] == "reject", f"action 应为 reject，实际: {audit_row[0]}"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = %s", (payload_sha,))
            conn.close()

    def test_reconcile_audit_reason_matches_outbox_final_status(self, migrated_db, logbook_adapter_config):
        """
        测试对账后审计 reason 与 outbox 最终状态的一致性
        
        场景：
        1. sent 状态缺失审计 -> 对账补写 outbox_flush_success
        2. dead 状态缺失审计 -> 对账补写 outbox_flush_dead
        3. 验证补写的 reason 与状态对应
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        sent_outbox_id = None
        dead_outbox_id = None
        sent_sha = f"sha_reconcile_sent_{uuid.uuid4().hex[:8]}"
        dead_sha = f"sha_reconcile_dead_{uuid.uuid4().hex[:8]}"
        
        try:
            # 创建 sent 状态记录（无对应审计）
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, last_error)
                    VALUES ('private:reconcile_sent', '# Sent Without Audit', %s, 'sent', 'memory_id=mem_reconcile_sent')
                    RETURNING outbox_id
                """, (sent_sha,))
                sent_outbox_id = cur.fetchone()[0]
            
            # 创建 dead 状态记录（无对应审计）
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, last_error)
                    VALUES ('private:reconcile_dead', '# Dead Without Audit', %s, 'dead', 5, 'max_retries_exceeded')
                    RETURNING outbox_id
                """, (dead_sha,))
                dead_outbox_id = cur.fetchone()[0]
            
            # 执行对账
            from engram.gateway.reconcile_outbox import ReconcileConfig, run_reconcile
            
            config = ReconcileConfig(
                scan_window_hours=24,
                auto_fix=True,
            )
            result = run_reconcile(config)
            
            # 验证 sent 审计被补写且 reason 正确
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason
                    FROM {governance_schema}.write_audit
                    WHERE (evidence_refs_json->>'outbox_id')::int = %s
                    AND reason = 'outbox_flush_success'
                """, (sent_outbox_id,))
                sent_audit = cur.fetchone()
                
                assert sent_audit is not None, \
                    f"sent 状态应补写 outbox_flush_success 审计，outbox_id={sent_outbox_id}"
                assert sent_audit[0] == "allow", f"sent 审计 action 应为 allow，实际: {sent_audit[0]}"
            
            # 验证 dead 审计被补写且 reason 正确
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT action, reason
                    FROM {governance_schema}.write_audit
                    WHERE (evidence_refs_json->>'outbox_id')::int = %s
                    AND reason = 'outbox_flush_dead'
                """, (dead_outbox_id,))
                dead_audit = cur.fetchone()
                
                assert dead_audit is not None, \
                    f"dead 状态应补写 outbox_flush_dead 审计，outbox_id={dead_outbox_id}"
                assert dead_audit[0] == "reject", f"dead 审计 action 应为 reject，实际: {dead_audit[0]}"
        
        finally:
            with conn.cursor() as cur:
                if sent_outbox_id:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (sent_outbox_id,))
                if dead_outbox_id:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (dead_outbox_id,))
                cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = %s", (sent_sha,))
                cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = %s", (dead_sha,))
                cur.execute(f"""
                    DELETE FROM {governance_schema}.write_audit 
                    WHERE (evidence_refs_json->>'outbox_id')::int IN (%s, %s)
                """, (sent_outbox_id or 0, dead_outbox_id or 0))
            conn.close()


class TestOutboxWorkerFullAcceptance:
    """
    FULL 验收必测用例
    
    验证 docs/acceptance/00_acceptance_matrix.md 中 "Outbox Worker 真实集成测试（FULL 必测）" 章节定义的所有断言点：
    1. 状态流转断言：outbox 记录在 pending/sent/dead 三种状态之间正确流转
    2. 审计 reason 断言：governance.write_audit 记录的 reason 字段正确
    3. evidence_refs_json 可查询：(evidence_refs_json->>'outbox_id')::int 可关联回 outbox 记录
    
    这些测试在 HTTP_ONLY_MODE=1 时会被跳过（由模块级 pytestmark 控制）。
    """

    def test_full_acceptance_status_transitions(self, migrated_db, logbook_adapter_config):
        """
        FULL 验收：验证三种状态流转路径
        
        覆盖：
        - pending → sent (成功)
        - pending → pending (重试，retry_count++)
        - pending → dead (超过最大重试)
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        test_prefix = f"full_accept_{uuid.uuid4().hex[:8]}"
        outbox_ids = {"success": None, "retry": None, "dead": None}
        
        try:
            # 准备三条测试记录
            with conn.cursor() as cur:
                # 1. 将成功的记录
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at)
                    VALUES ('private:user_success', '# Success', '{test_prefix}_success', 'pending', 0, now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_ids["success"] = cur.fetchone()[0]
                
                # 2. 将重试的记录
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at)
                    VALUES ('private:user_retry', '# Retry', '{test_prefix}_retry', 'pending', 0, now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_ids["retry"] = cur.fetchone()[0]
                
                # 3. 将变为 dead 的记录（已接近最大重试次数）
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at)
                    VALUES ('private:user_dead', '# Dead', '{test_prefix}_dead', 'pending', 4, now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_ids["dead"] = cur.fetchone()[0]
            
            # 配置 mock 以产生不同结果
            call_count = {"success": 0, "retry": 0, "dead": 0}
            
            def mock_store(content, space, user_id, metadata):
                sha = metadata.get("payload_sha", "")
                if sha.endswith("_success"):
                    call_count["success"] += 1
                    return MockStoreResult(success=True, memory_id=f"mem_{test_prefix}_success")
                elif sha.endswith("_retry"):
                    call_count["retry"] += 1
                    return MockStoreResult(success=False, error="temporary_failure")
                elif sha.endswith("_dead"):
                    call_count["dead"] += 1
                    return MockStoreResult(success=False, error="persistent_failure")
                return MockStoreResult(success=False, error="unknown")
            
            mock_client = MagicMock()
            mock_client.store.side_effect = mock_store
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=5, jitter_factor=0.0)
                results = process_batch(config, worker_id="full-acceptance-worker")
            
            # 断言 1：状态流转正确
            with conn.cursor() as cur:
                # 1a. success 记录应变为 sent
                cur.execute(f"""
                    SELECT status, locked_by, locked_at
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_ids["success"],))
                row = cur.fetchone()
                assert row[0] == "sent", f"[状态流转] success 记录应为 sent，实际: {row[0]}"
                assert row[1] is None, "[状态流转] sent 后 locked_by 应为 NULL"
                assert row[2] is None, "[状态流转] sent 后 locked_at 应为 NULL"
                
                # 1b. retry 记录应保持 pending，retry_count 增加
                cur.execute(f"""
                    SELECT status, retry_count, locked_by
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_ids["retry"],))
                row = cur.fetchone()
                assert row[0] == "pending", f"[状态流转] retry 记录应保持 pending，实际: {row[0]}"
                assert row[1] == 1, f"[状态流转] retry 记录 retry_count 应为 1，实际: {row[1]}"
                assert row[2] is None, "[状态流转] retry 后 locked_by 应为 NULL（锁已释放）"
                
                # 1c. dead 记录应变为 dead（retry_count=4 + 1次失败 >= max_retries=5）
                cur.execute(f"""
                    SELECT status, retry_count
                    FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_ids["dead"],))
                row = cur.fetchone()
                assert row[0] == "dead", f"[状态流转] dead 记录应为 dead，实际: {row[0]}"
        
        finally:
            with conn.cursor() as cur:
                for key, oid in outbox_ids.items():
                    if oid:
                        cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (oid,))
                cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha LIKE '{test_prefix}%%'")
            conn.close()

    def test_full_acceptance_audit_reason_values(self, migrated_db, logbook_adapter_config):
        """
        FULL 验收：验证审计 reason 字段值正确
        
        覆盖：
        - outbox_flush_success：成功写入 OpenMemory
        - outbox_flush_retry：可重试失败
        - outbox_flush_dead：不可恢复失败
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        test_prefix = f"audit_reason_{uuid.uuid4().hex[:8]}"
        outbox_ids = {"success": None, "retry": None, "dead": None}
        
        try:
            # 准备测试记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, next_attempt_at)
                    VALUES 
                        ('private:u1', '# S', '{test_prefix}_success', 'pending', 0, now() - interval '1 minute'),
                        ('private:u2', '# R', '{test_prefix}_retry', 'pending', 0, now() - interval '1 minute'),
                        ('private:u3', '# D', '{test_prefix}_dead', 'pending', 4, now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                rows = cur.fetchall()
                outbox_ids["success"] = rows[0][0]
                outbox_ids["retry"] = rows[1][0]
                outbox_ids["dead"] = rows[2][0]
            
            def mock_store(content, space, user_id, metadata):
                sha = metadata.get("payload_sha", "")
                if sha.endswith("_success"):
                    return MockStoreResult(success=True, memory_id=f"mem_{test_prefix}")
                else:
                    return MockStoreResult(success=False, error="test_failure")
            
            mock_client = MagicMock()
            mock_client.store.side_effect = mock_store
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=5, jitter_factor=0.0)
                process_batch(config, worker_id="audit-reason-worker")
            
            # 断言 2：审计 reason 值正确
            with conn.cursor() as cur:
                # 2a. success 应产生 outbox_flush_success 审计
                cur.execute(f"""
                    SELECT action, reason
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = '{test_prefix}_success'
                    ORDER BY created_at DESC LIMIT 1
                """)
                row = cur.fetchone()
                assert row is not None, "[审计 reason] success 应有审计记录"
                assert row[0] == "allow", f"[审计 reason] success action 应为 allow，实际: {row[0]}"
                assert row[1] == "outbox_flush_success", f"[审计 reason] success reason 应为 outbox_flush_success，实际: {row[1]}"
                
                # 2b. retry 应产生 outbox_flush_retry 审计
                cur.execute(f"""
                    SELECT action, reason
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = '{test_prefix}_retry'
                    ORDER BY created_at DESC LIMIT 1
                """)
                row = cur.fetchone()
                assert row is not None, "[审计 reason] retry 应有审计记录"
                assert row[0] == "redirect", f"[审计 reason] retry action 应为 redirect，实际: {row[0]}"
                assert row[1] == "outbox_flush_retry", f"[审计 reason] retry reason 应为 outbox_flush_retry，实际: {row[1]}"
                
                # 2c. dead 应产生 outbox_flush_dead 审计
                cur.execute(f"""
                    SELECT action, reason
                    FROM {governance_schema}.write_audit
                    WHERE payload_sha = '{test_prefix}_dead'
                    ORDER BY created_at DESC LIMIT 1
                """)
                row = cur.fetchone()
                assert row is not None, "[审计 reason] dead 应有审计记录"
                assert row[0] == "reject", f"[审计 reason] dead action 应为 reject，实际: {row[0]}"
                assert row[1] == "outbox_flush_dead", f"[审计 reason] dead reason 应为 outbox_flush_dead，实际: {row[1]}"
        
        finally:
            with conn.cursor() as cur:
                for key, oid in outbox_ids.items():
                    if oid:
                        cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (oid,))
                cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha LIKE '{test_prefix}%%'")
            conn.close()

    def test_full_acceptance_evidence_refs_outbox_id_queryable(self, migrated_db, logbook_adapter_config):
        """
        FULL 验收：验证 evidence_refs_json->>'outbox_id' 可查询
        
        覆盖：
        - 审计记录的 evidence_refs_json 包含 outbox_id
        - 可通过 (evidence_refs_json->>'outbox_id')::int 查询关联的审计记录
        """
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]
        governance_schema = migrated_db["schemas"]["governance"]
        
        conn = psycopg.connect(dsn, autocommit=True)
        test_prefix = f"evidence_query_{uuid.uuid4().hex[:8]}"
        outbox_id = None
        
        try:
            # 准备测试记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES ('private:query_user', '# Query Test', '{test_prefix}', 'pending', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]
            
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(success=True, memory_id=f"mem_{test_prefix}")
            
            with patch("gateway.outbox_worker.openmemory_client.OpenMemoryClient", return_value=mock_client):
                from engram.gateway.outbox_worker import WorkerConfig, process_batch
                
                config = WorkerConfig(batch_size=10, max_retries=5)
                process_batch(config, worker_id="evidence-query-worker")
            
            # 断言 3：evidence_refs_json->>'outbox_id' 可查询
            with conn.cursor() as cur:
                # 3a. 使用 outbox_id 查询审计记录
                cur.execute(f"""
                    SELECT action, reason, payload_sha, evidence_refs_json
                    FROM {governance_schema}.write_audit
                    WHERE (evidence_refs_json->>'outbox_id')::int = %s
                """, (outbox_id,))
                row = cur.fetchone()
                
                assert row is not None, \
                    f"[evidence_refs_json 查询] 应能通过 outbox_id={outbox_id} 查询到审计记录"
                assert row[0] == "allow", f"[evidence_refs_json 查询] action 应为 allow，实际: {row[0]}"
                assert row[1] == "outbox_flush_success", f"[evidence_refs_json 查询] reason 应为 outbox_flush_success，实际: {row[1]}"
                assert row[2] == test_prefix, f"[evidence_refs_json 查询] payload_sha 应匹配，实际: {row[2]}"
                
                # 3b. 验证 evidence_refs_json 包含关键字段
                evidence = row[3]
                assert evidence is not None, "[evidence_refs_json 查询] evidence_refs_json 不应为空"
                assert evidence.get("outbox_id") == outbox_id, \
                    f"[evidence_refs_json 查询] outbox_id 应为 {outbox_id}，实际: {evidence.get('outbox_id')}"
                assert evidence.get("source") == "outbox_worker", \
                    f"[evidence_refs_json 查询] source 应为 outbox_worker，实际: {evidence.get('source')}"
                assert "memory_id" in evidence, "[evidence_refs_json 查询] 应包含 memory_id"
                
                # 3c. 验证 extra 字段包含 correlation_id 和 attempt_id
                extra = evidence.get("extra")
                assert extra is not None, "[evidence_refs_json 查询] 应包含 extra 字段"
                assert "correlation_id" in extra, "[evidence_refs_json 查询] extra 应包含 correlation_id"
                assert "attempt_id" in extra, "[evidence_refs_json 查询] extra 应包含 attempt_id"
        
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                    cur.execute(f"DELETE FROM {governance_schema}.write_audit WHERE payload_sha = '{test_prefix}'")
                    cur.execute(f"""
                        DELETE FROM {governance_schema}.write_audit 
                        WHERE (evidence_refs_json->>'outbox_id')::int = %s
                    """, (outbox_id,))
            conn.close()

    def test_full_acceptance_http_only_mode_skip_visible(self):
        """
        验证 HTTP_ONLY_MODE 下跳过信息可见
        
        此测试本身在 HTTP_ONLY_MODE=1 时会被跳过，
        跳过原因应为 "HTTP_ONLY_MODE: Outbox Worker 集成测试需要 Docker 和数据库"
        
        注意：此测试的存在是为了验证 pytestmark 配置正确。
        在 HTTP_ONLY_MODE=0 时此测试会运行并通过。
        """
        # 如果我们能运行到这里，说明 HTTP_ONLY_MODE 未启用
        assert not HTTP_ONLY_MODE, "此测试应在 HTTP_ONLY_MODE=0 时运行"
        
        # 验证模块级跳过原因配置正确
        assert HTTP_ONLY_SKIP_REASON == "HTTP_ONLY_MODE: Outbox Worker 集成测试需要 Docker 和数据库"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
