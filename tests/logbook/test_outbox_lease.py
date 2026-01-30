# -*- coding: utf-8 -*-
"""
Outbox Lease 协议函数单元测试

**测试归属声明**：本文件只测试 `engram_logbook.outbox` 的原语契约，
包括 SQL 行为、锁机制、状态转换等底层实现细节。
Worker 流程语义测试请参见 Gateway 测试：
- gateway/tests/test_outbox_worker.py
- gateway/tests/test_outbox_worker_integration.py

测试:
- claim_outbox: 并发安全获取任务
- ack_sent: 确认发送成功
- fail_retry: 失败重试
- mark_dead_by_worker: 标记死信
- statement_timeout: 连接级超时设置
"""

import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
import psycopg


class TestGetConnectionStatementTimeout:
    """get_connection 的 statement_timeout 功能测试"""

    def test_statement_timeout_from_parameter(self, migrated_db):
        """通过参数设置 statement_timeout"""
        dsn = migrated_db["dsn"]

        from engram.logbook.db import get_connection

        # 使用参数设置 statement_timeout 为 1000ms
        conn = get_connection(dsn=dsn, statement_timeout_ms=1000)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW statement_timeout")
                result = cur.fetchone()[0]
                # PostgreSQL 返回格式可能是 "1s" 或 "1000ms"
                assert result in ("1s", "1000ms"), f"statement_timeout 应为 1s 或 1000ms，实际: {result}"
        finally:
            conn.close()

    def test_statement_timeout_from_env(self, migrated_db, monkeypatch):
        """通过环境变量设置 statement_timeout"""
        dsn = migrated_db["dsn"]
        
        # 设置环境变量
        monkeypatch.setenv("ENGRAM_PG_STATEMENT_TIMEOUT_MS", "2000")

        from engram.logbook.db import get_connection

        conn = get_connection(dsn=dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW statement_timeout")
                result = cur.fetchone()[0]
                assert result in ("2s", "2000ms"), f"statement_timeout 应为 2s 或 2000ms，实际: {result}"
        finally:
            conn.close()

    def test_statement_timeout_parameter_overrides_env(self, migrated_db, monkeypatch):
        """参数优先于环境变量"""
        dsn = migrated_db["dsn"]
        
        # 设置环境变量
        monkeypatch.setenv("ENGRAM_PG_STATEMENT_TIMEOUT_MS", "5000")

        from engram.logbook.db import get_connection

        # 参数设置为 1000ms，应覆盖环境变量的 5000ms
        conn = get_connection(dsn=dsn, statement_timeout_ms=1000)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW statement_timeout")
                result = cur.fetchone()[0]
                assert result in ("1s", "1000ms"), f"statement_timeout 应为 1s，实际: {result}"
        finally:
            conn.close()

    def test_statement_timeout_not_set_when_env_empty(self, migrated_db, monkeypatch):
        """环境变量未设置时不修改默认值"""
        dsn = migrated_db["dsn"]
        
        # 确保环境变量未设置
        monkeypatch.delenv("ENGRAM_PG_STATEMENT_TIMEOUT_MS", raising=False)

        from engram.logbook.db import get_connection

        conn = get_connection(dsn=dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW statement_timeout")
                result = cur.fetchone()[0]
                # 默认值通常是 0（无限制）
                assert result == "0", f"statement_timeout 应为默认值 0，实际: {result}"
        finally:
            conn.close()

    def test_statement_timeout_triggers_query_canceled(self, migrated_db):
        """验证超时触发 QueryCanceled 异常"""
        dsn = migrated_db["dsn"]

        from engram.logbook.db import get_connection

        # 设置极短的超时（10ms）
        conn = get_connection(dsn=dsn, statement_timeout_ms=10)
        try:
            with pytest.raises(psycopg.errors.QueryCanceled) as exc_info:
                with conn.cursor() as cur:
                    # 执行长时间查询（100ms）
                    cur.execute("SELECT pg_sleep(0.1)")
            
            # 验证错误类型
            assert "statement timeout" in str(exc_info.value).lower() or exc_info.value.sqlstate == "57014"
        finally:
            conn.close()

    def test_statement_timeout_invalid_env_ignored(self, migrated_db, monkeypatch):
        """无效的环境变量值被忽略"""
        dsn = migrated_db["dsn"]
        
        # 设置无效值
        monkeypatch.setenv("ENGRAM_PG_STATEMENT_TIMEOUT_MS", "not_a_number")

        from engram.logbook.db import get_connection

        # 应该不抛出异常，使用默认值
        conn = get_connection(dsn=dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW statement_timeout")
                result = cur.fetchone()[0]
                # 应该是默认值
                assert result == "0", f"无效环境变量应被忽略，使用默认值 0，实际: {result}"
        finally:
            conn.close()


class TestCheckDedup:
    """check_dedup 函数测试 - 幂等去重检查"""

    def test_check_dedup_no_match(self, migrated_db):
        """无匹配记录时返回 None"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        try:
            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import check_dedup
                result = check_dedup(target_space="team:test", payload_sha="nonexistent_sha")

                assert result is None
        finally:
            conn.close()

    def test_check_dedup_sent_record_found(self, migrated_db):
        """找到 sent 状态的记录"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                # 插入一条 sent 状态的记录
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, last_error)
                    VALUES ('team:test', 'payload content', 'sha_sent_123', 'sent', 'memory_id=mem_abc')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import check_dedup
                result = check_dedup(target_space="team:test", payload_sha="sha_sent_123")

                assert result is not None
                assert result["outbox_id"] == outbox_id
                assert result["target_space"] == "team:test"
                assert result["payload_sha"] == "sha_sent_123"
                assert result["status"] == "sent"
                assert result["last_error"] == "memory_id=mem_abc"
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_check_dedup_ignores_pending(self, migrated_db):
        """pending 状态的记录不应被去重检测到"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                # 插入一条 pending 状态的记录
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status)
                    VALUES ('team:test', 'payload', 'sha_pending_456', 'pending')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import check_dedup
                result = check_dedup(target_space="team:test", payload_sha="sha_pending_456")

                # pending 状态不应被检测到
                assert result is None
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_check_dedup_ignores_dead(self, migrated_db):
        """dead 状态的记录不应被去重检测到"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                # 插入一条 dead 状态的记录
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status)
                    VALUES ('team:test', 'payload', 'sha_dead_789', 'dead')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import check_dedup
                result = check_dedup(target_space="team:test", payload_sha="sha_dead_789")

                # dead 状态不应被检测到
                assert result is None
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_check_dedup_different_target_space(self, migrated_db):
        """不同 target_space 不应匹配"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                # 插入一条 sent 状态的记录
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status)
                    VALUES ('team:project_a', 'payload', 'sha_common', 'sent')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import check_dedup
                # 查询不同的 target_space
                result = check_dedup(target_space="team:project_b", payload_sha="sha_common")

                # 不同 target_space 不应匹配
                assert result is None
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()


class TestClaimOutbox:
    """claim_outbox 函数测试"""

    def test_claim_outbox_basic(self, migrated_db):
        """基本功能: 获取 pending 记录并设置锁"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        # 插入测试数据
        conn = psycopg.connect(dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES
                        ('team:test', 'payload1', 'sha1', 'pending', now() - interval '1 minute'),
                        ('team:test', 'payload2', 'sha2', 'pending', now() - interval '2 minutes')
                    RETURNING outbox_id
                """)
                ids = [row[0] for row in cur.fetchall()]

            # Mock get_connection 返回正确的连接
            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import claim_outbox
                results = claim_outbox(worker_id="worker-1", limit=10, lease_seconds=60)

                assert len(results) == 2
                for r in results:
                    assert r["locked_by"] == "worker-1"
                    assert r["locked_at"] is not None
                    assert r["status"] == "pending"

            # 验证数据库中锁已设置
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT locked_by, locked_at FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = ANY(%s)
                """, (ids,))
                rows = cur.fetchall()
                for row in rows:
                    assert row[0] == "worker-1"
                    assert row[1] is not None
        finally:
            # 清理
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = ANY(%s)", (ids,))
            conn.close()

    def test_claim_outbox_respects_limit(self, migrated_db):
        """限制返回数量"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        ids = []
        try:
            with conn.cursor() as cur:
                for i in range(5):
                    cur.execute(f"""
                        INSERT INTO {logbook_schema}.outbox_memory
                            (target_space, payload_md, payload_sha, status, next_attempt_at)
                        VALUES ('team:test', 'payload{i}', 'sha{i}', 'pending', now() - interval '1 minute')
                        RETURNING outbox_id
                    """)
                    ids.append(cur.fetchone()[0])

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import claim_outbox
                results = claim_outbox(worker_id="worker-1", limit=3, lease_seconds=60)

                assert len(results) == 3
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = ANY(%s)", (ids,))
            conn.close()

    def test_claim_outbox_skips_future_attempts(self, migrated_db):
        """跳过 next_attempt_at 在未来的记录"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        ids = []
        try:
            with conn.cursor() as cur:
                # 一条可用，一条在未来
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at)
                    VALUES
                        ('team:test', 'ready', 'sha1', 'pending', now() - interval '1 minute'),
                        ('team:test', 'future', 'sha2', 'pending', now() + interval '1 hour')
                    RETURNING outbox_id
                """)
                ids = [row[0] for row in cur.fetchall()]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import claim_outbox
                results = claim_outbox(worker_id="worker-1", limit=10, lease_seconds=60)

                assert len(results) == 1
                assert results[0]["payload_md"] == "ready"
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = ANY(%s)", (ids,))
            conn.close()

    def test_claim_outbox_skips_locked_records(self, migrated_db):
        """跳过已被锁定且未过期的记录"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        ids = []
        try:
            with conn.cursor() as cur:
                # 一条未锁定，一条已锁定（未过期）
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at, locked_by, locked_at)
                    VALUES
                        ('team:test', 'unlocked', 'sha1', 'pending', now() - interval '1 minute', NULL, NULL),
                        ('team:test', 'locked', 'sha2', 'pending', now() - interval '1 minute', 'other-worker', now())
                    RETURNING outbox_id
                """)
                ids = [row[0] for row in cur.fetchall()]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import claim_outbox
                results = claim_outbox(worker_id="worker-1", limit=10, lease_seconds=60)

                assert len(results) == 1
                assert results[0]["payload_md"] == "unlocked"
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = ANY(%s)", (ids,))
            conn.close()

    def test_claim_outbox_reclaims_expired_lease(self, migrated_db):
        """重新获取已过期的锁"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        ids = []
        try:
            with conn.cursor() as cur:
                # 锁已过期（locked_at 在 2 分钟前，lease_seconds=60）
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at, locked_by, locked_at)
                    VALUES
                        ('team:test', 'expired', 'sha1', 'pending', now() - interval '1 minute', 
                         'dead-worker', now() - interval '2 minutes')
                    RETURNING outbox_id
                """)
                ids = [cur.fetchone()[0]]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import claim_outbox
                results = claim_outbox(worker_id="worker-2", limit=10, lease_seconds=60)

                assert len(results) == 1
                assert results[0]["locked_by"] == "worker-2"
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = ANY(%s)", (ids,))
            conn.close()

    def test_claim_outbox_lease_seconds_zero_can_reclaim(self, migrated_db):
        """边界测试: lease_seconds=0 时，刚被锁定的记录也可以被 reclaim"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        ids = []
        try:
            with conn.cursor() as cur:
                # 刚刚被 worker-1 锁定（locked_at = now()）
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at, locked_by, locked_at)
                    VALUES
                        ('team:test', 'just_locked', 'sha_zero', 'pending', now() - interval '1 minute',
                         'worker-1', now())
                    RETURNING outbox_id
                """)
                ids = [cur.fetchone()[0]]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import claim_outbox
                # lease_seconds=0 意味着任何已锁定的记录都被认为已过期
                results = claim_outbox(worker_id="worker-2", limit=10, lease_seconds=0)

                # 刚刚锁定的记录应该可以被 reclaim（因为 lease_seconds=0）
                assert len(results) == 1
                assert results[0]["locked_by"] == "worker-2"
                assert results[0]["outbox_id"] == ids[0]
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = ANY(%s)", (ids,))
            conn.close()

    def test_claim_outbox_lease_seconds_one_cannot_reclaim_fresh(self, migrated_db):
        """边界测试: lease_seconds=1 时，刚被锁定的记录不可被 reclaim"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        ids = []
        try:
            with conn.cursor() as cur:
                # 刚刚被 worker-1 锁定（locked_at = now()）
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, next_attempt_at, locked_by, locked_at)
                    VALUES
                        ('team:test', 'just_locked', 'sha_one', 'pending', now() - interval '1 minute',
                         'worker-1', now())
                    RETURNING outbox_id
                """)
                ids = [cur.fetchone()[0]]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import claim_outbox
                # lease_seconds=1 时，刚刚锁定的记录（locked_at = now()）不应被 reclaim
                results = claim_outbox(worker_id="worker-2", limit=10, lease_seconds=1)

                # 刚刚锁定的记录不应被 reclaim（因为 lease 还未过期）
                claimed_ids = [r["outbox_id"] for r in results]
                assert ids[0] not in claimed_ids, "lease_seconds=1 时，刚锁定的记录不应被 reclaim"
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = ANY(%s)", (ids,))
            conn.close()


class TestAckSent:
    """ack_sent 函数测试"""

    def test_ack_sent_success(self, migrated_db):
        """成功确认发送"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, locked_by, locked_at)
                    VALUES ('team:test', 'payload', 'sha1', 'pending', 'worker-1', now())
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import ack_sent
                result = ack_sent(outbox_id, worker_id="worker-1", memory_id="mem-123")

                assert result is True

            # 验证状态变更
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, locked_by, locked_at, last_error
                    FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                assert row[0] == "sent"
                assert row[1] is None  # 锁已释放
                assert row[2] is None
                assert row[3] == "memory_id=mem-123"
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_ack_sent_wrong_worker(self, migrated_db):
        """错误的 worker_id 无法确认"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, locked_by, locked_at)
                    VALUES ('team:test', 'payload', 'sha1', 'pending', 'worker-1', now())
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import ack_sent
                result = ack_sent(outbox_id, worker_id="wrong-worker")

                assert result is False

            # 验证状态未变
            with conn.cursor() as cur:
                cur.execute(f"SELECT status FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                assert cur.fetchone()[0] == "pending"
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()


class TestFailRetry:
    """fail_retry 函数测试"""

    def test_fail_retry_with_specified_time(self, migrated_db):
        """使用指定的重试时间"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, locked_by, locked_at)
                    VALUES ('team:test', 'payload', 'sha1', 'pending', 0, 'worker-1', now())
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            next_time = datetime.now(timezone.utc) + timedelta(hours=1)

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import fail_retry
                result = fail_retry(outbox_id, worker_id="worker-1", error="timeout", next_attempt_at=next_time)

                assert result is True

            # 验证更新
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, retry_count, last_error, locked_by, locked_at
                    FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                assert row[0] == "pending"  # 状态保持
                assert row[1] == 1  # retry_count 增加
                assert row[2] == "timeout"
                assert row[3] is None  # 锁已释放
                assert row[4] is None
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_fail_retry_stores_next_attempt_at(self, migrated_db):
        """验证 next_attempt_at 正确存储"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, locked_by, locked_at, next_attempt_at)
                    VALUES ('team:test', 'payload', 'sha1', 'pending', 2, 'worker-1', now(), now())
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            # 设置一个确定的下次重试时间（2小时后）
            next_time = datetime.now(timezone.utc) + timedelta(hours=2)

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import fail_retry
                result = fail_retry(outbox_id, worker_id="worker-1", error="error", next_attempt_at=next_time)

                assert result is True

            # 验证 retry_count 增加到 3 且 next_attempt_at 正确存储
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT retry_count, next_attempt_at 
                    FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                assert row[0] == 3
                # 验证 next_attempt_at 在预期范围内（允许几秒误差）
                stored_time = row[1]
                if stored_time.tzinfo is None:
                    stored_time = stored_time.replace(tzinfo=timezone.utc)
                time_diff = abs((stored_time - next_time).total_seconds())
                assert time_diff < 5, f"next_attempt_at 应接近预期值，差异: {time_diff}s"
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_fail_retry_with_iso_string(self, migrated_db):
        """验证 ISO 字符串格式的 next_attempt_at"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, locked_by, locked_at, next_attempt_at)
                    VALUES ('team:test', 'payload', 'sha1', 'pending', 0, 'worker-1', now(), now())
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            # 使用 ISO 字符串格式
            next_time = datetime.now(timezone.utc) + timedelta(minutes=30)
            next_time_iso = next_time.isoformat()

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import fail_retry
                result = fail_retry(outbox_id, worker_id="worker-1", error="error", next_attempt_at=next_time_iso)

                assert result is True

            # 验证 next_attempt_at 正确存储
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT next_attempt_at FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s
                """, (outbox_id,))
                stored_time = cur.fetchone()[0]
                if stored_time.tzinfo is None:
                    stored_time = stored_time.replace(tzinfo=timezone.utc)
                time_diff = abs((stored_time - next_time).total_seconds())
                assert time_diff < 5, f"next_attempt_at 应接近预期值，差异: {time_diff}s"
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_fail_retry_wrong_worker(self, migrated_db):
        """错误的 worker_id 无法更新"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, retry_count, locked_by, locked_at)
                    VALUES ('team:test', 'payload', 'sha1', 'pending', 0, 'worker-1', now())
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            next_time = datetime.now(timezone.utc) + timedelta(hours=1)

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import fail_retry
                result = fail_retry(outbox_id, worker_id="wrong-worker", error="error", next_attempt_at=next_time)

                assert result is False
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()


class TestMarkDeadByWorker:
    """mark_dead_by_worker 函数测试"""

    def test_mark_dead_success(self, migrated_db):
        """成功标记死信"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, locked_by, locked_at)
                    VALUES ('team:test', 'payload', 'sha1', 'pending', 'worker-1', now())
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import mark_dead_by_worker
                result = mark_dead_by_worker(outbox_id, worker_id="worker-1", error="max retries exceeded")

                assert result is True

            # 验证状态变更
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT status, last_error, locked_by, locked_at
                    FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                assert row[0] == "dead"
                assert row[1] == "max retries exceeded"
                assert row[2] is None  # 锁已释放
                assert row[3] is None
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_mark_dead_wrong_worker(self, migrated_db):
        """错误的 worker_id 无法标记"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, locked_by, locked_at)
                    VALUES ('team:test', 'payload', 'sha1', 'pending', 'worker-1', now())
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import mark_dead_by_worker
                result = mark_dead_by_worker(outbox_id, worker_id="wrong-worker", error="error")

                assert result is False

            # 验证状态未变
            with conn.cursor() as cur:
                cur.execute(f"SELECT status FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
                assert cur.fetchone()[0] == "pending"
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_mark_dead_already_sent(self, migrated_db):
        """已发送的记录无法标记为死信"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status)
                    VALUES ('team:test', 'payload', 'sha1', 'sent')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import mark_dead_by_worker
                result = mark_dead_by_worker(outbox_id, worker_id="worker-1", error="error")

                assert result is False
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()


class TestRenewLease:
    """renew_lease 函数测试"""

    def test_renew_lease_success(self, migrated_db):
        """成功续期 Lease"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            # 插入一条已锁定的记录
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, locked_by, locked_at)
                    VALUES ('team:test', 'payload', 'sha_renew_1', 'pending', 'worker-1', now() - interval '30 seconds')
                    RETURNING outbox_id, locked_at
                """)
                row = cur.fetchone()
                outbox_id = row[0]
                old_locked_at = row[1]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import renew_lease
                result = renew_lease(outbox_id, worker_id="worker-1")

                assert result is True

            # 验证 locked_at 已更新
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT locked_by, locked_at FROM {logbook_schema}.outbox_memory
                    WHERE outbox_id = %s
                """, (outbox_id,))
                row = cur.fetchone()
                assert row[0] == "worker-1"
                assert row[1] > old_locked_at, "locked_at 应该被更新为更新的时间"
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_renew_lease_wrong_worker(self, migrated_db):
        """错误的 worker_id 无法续期"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, locked_by, locked_at)
                    VALUES ('team:test', 'payload', 'sha_renew_2', 'pending', 'worker-1', now())
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import renew_lease
                result = renew_lease(outbox_id, worker_id="wrong-worker")

                assert result is False
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_renew_lease_not_pending(self, migrated_db):
        """非 pending 状态的记录无法续期"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, locked_by, locked_at)
                    VALUES ('team:test', 'payload', 'sha_renew_3', 'sent', 'worker-1', now())
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import renew_lease
                result = renew_lease(outbox_id, worker_id="worker-1")

                assert result is False
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_renew_lease_prevents_reclaim(self, migrated_db):
        """续期后的记录不会被其他 worker reclaim"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_id = None
        try:
            # 插入一条记录，锁定时间在 30 秒前（lease_seconds=60 时不会过期）
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, locked_by, locked_at, next_attempt_at)
                    VALUES ('team:test', 'payload', 'sha_renew_4', 'pending', 'worker-1', now() - interval '30 seconds', now() - interval '1 minute')
                    RETURNING outbox_id
                """)
                outbox_id = cur.fetchone()[0]

            # Worker-1 续期
            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import renew_lease
                result = renew_lease(outbox_id, worker_id="worker-1")
                assert result is True

            # Worker-2 尝试 claim（lease_seconds=60，续期后不应能获取）
            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import claim_outbox
                results = claim_outbox(worker_id="worker-2", limit=10, lease_seconds=60)

                # 续期后的记录不应被 worker-2 获取
                claimed_ids = [r["outbox_id"] for r in results]
                assert outbox_id not in claimed_ids, "续期后的记录不应被其他 worker claim"
        finally:
            if outbox_id:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()


class TestRenewLeaseBatch:
    """renew_lease_batch 函数测试"""

    def test_renew_lease_batch_success(self, migrated_db):
        """批量续期成功"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_ids = []
        try:
            # 插入多条记录
            for i in range(3):
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {logbook_schema}.outbox_memory
                            (target_space, payload_md, payload_sha, status, locked_by, locked_at)
                        VALUES ('team:test', 'payload{i}', 'sha_batch_{i}', 'pending', 'worker-1', now() - interval '30 seconds')
                        RETURNING outbox_id
                    """)
                    outbox_ids.append(cur.fetchone()[0])

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import renew_lease_batch
                count = renew_lease_batch(outbox_ids, worker_id="worker-1")

                assert count == 3
        finally:
            for outbox_id in outbox_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_renew_lease_batch_partial(self, migrated_db):
        """批量续期部分成功（部分记录属于其他 worker）"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        conn = psycopg.connect(dsn, autocommit=True)
        outbox_ids = []
        try:
            # 插入 2 条属于 worker-1，1 条属于 worker-2
            for i in range(2):
                with conn.cursor() as cur:
                    cur.execute(f"""
                        INSERT INTO {logbook_schema}.outbox_memory
                            (target_space, payload_md, payload_sha, status, locked_by, locked_at)
                        VALUES ('team:test', 'payload', 'sha_partial_{i}', 'pending', 'worker-1', now())
                        RETURNING outbox_id
                    """)
                    outbox_ids.append(cur.fetchone()[0])

            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {logbook_schema}.outbox_memory
                        (target_space, payload_md, payload_sha, status, locked_by, locked_at)
                    VALUES ('team:test', 'payload', 'sha_partial_other', 'pending', 'worker-2', now())
                    RETURNING outbox_id
                """)
                outbox_ids.append(cur.fetchone()[0])

            with patch('engram_logbook.outbox.get_connection') as mock_get_conn:
                mock_conn = psycopg.connect(dsn, autocommit=False)
                with mock_conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {logbook_schema}")
                mock_get_conn.return_value = mock_conn

                from engram.logbook.outbox import renew_lease_batch
                count = renew_lease_batch(outbox_ids, worker_id="worker-1")

                # 只应续期属于 worker-1 的 2 条
                assert count == 2
        finally:
            for outbox_id in outbox_ids:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {logbook_schema}.outbox_memory WHERE outbox_id = %s", (outbox_id,))
            conn.close()

    def test_renew_lease_batch_empty(self, migrated_db):
        """空列表返回 0"""
        dsn = migrated_db["dsn"]
        logbook_schema = migrated_db["schemas"]["logbook"]

        from engram.logbook.outbox import renew_lease_batch
        count = renew_lease_batch([], worker_id="worker-1")
        assert count == 0
