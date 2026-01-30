# -*- coding: utf-8 -*-
"""
Outbox Worker 测试

**测试归属声明**：本文件只测试 worker 流程语义，不重复验证 SQL/锁实现细节。
SQL/锁等底层原语契约测试请参见 Logbook 测试：
- logbook_postgres/scripts/tests/test_outbox_lease.py

测试覆盖:
- claim_outbox 调用形态（worker_id, limit, lease_seconds）
- space 参数正确传递给 OpenMemory
- metadata 包含 outbox_id/payload_sha/target_space
- ack_sent / fail_retry / mark_dead 调用形态
- 成功/失败/重试逻辑
"""

import pytest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import MagicMock, patch, call, ANY

from engram.gateway.outbox_worker import (
    WorkerConfig,
    ProcessResult,
    calculate_backoff_with_jitter,
    process_single_item,
    process_batch,
    run_once,
)
from engram.gateway.logbook_adapter import OutboxItem


# ======================== 测试数据结构 ========================

@dataclass
class MockStoreResult:
    """模拟 OpenMemory 存储结果"""
    success: bool
    memory_id: Optional[str] = None
    error: Optional[str] = None


def make_outbox_item(
    outbox_id: int = 1,
    item_id: Optional[int] = None,
    target_space: str = "private:user_001",
    payload_md: str = "# Test Memory",
    payload_sha: str = "a" * 64,
    retry_count: int = 0,
) -> OutboxItem:
    """创建测试用 OutboxItem"""
    return OutboxItem(
        outbox_id=outbox_id,
        item_id=item_id,
        target_space=target_space,
        payload_md=payload_md,
        payload_sha=payload_sha,
        retry_count=retry_count,
    )


# ======================== 退避计算测试 ========================

class TestBackoffCalculation:
    """退避计算测试"""

    def test_first_retry_base_backoff(self):
        """首次重试使用基础退避时间"""
        backoff = calculate_backoff_with_jitter(
            retry_count=0,
            base_seconds=60,
            max_seconds=3600,
            jitter_factor=0.0,
        )
        assert backoff == 60

    def test_exponential_backoff(self):
        """指数退避验证"""
        for retry in range(5):
            backoff = calculate_backoff_with_jitter(
                retry_count=retry,
                base_seconds=60,
                max_seconds=3600,
                jitter_factor=0.0,
            )
            expected = min(60 * (2 ** retry), 3600)
            assert backoff == expected, f"retry={retry} 应为 {expected}"

    def test_max_backoff_cap(self):
        """最大退避时间限制"""
        backoff = calculate_backoff_with_jitter(
            retry_count=10,
            base_seconds=60,
            max_seconds=3600,
            jitter_factor=0.0,
        )
        assert backoff == 3600

    def test_jitter_adds_variance(self):
        """抖动因子添加随机性"""
        results = set()
        for _ in range(100):
            backoff = calculate_backoff_with_jitter(
                retry_count=1,
                base_seconds=100,
                max_seconds=1000,
                jitter_factor=0.3,
            )
            results.add(backoff)
        
        assert len(results) > 1, "抖动应产生不同的退避值"
        
        for b in results:
            assert 140 <= b <= 260, f"退避值 {b} 应在合理范围内"


# ======================== Dedupe 测试 ========================

class TestDeduplication:
    """去重 (dedupe) 测试"""

    @pytest.fixture
    def config(self):
        return WorkerConfig(max_retries=3, jitter_factor=0.0)

    def test_dedup_hit_skips_openmemory_call(self, config):
        """存在已成功写入的记录时跳过 OpenMemory 调用"""
        item = make_outbox_item(
            outbox_id=101,
            target_space="team:project",
            payload_sha="sha_already_sent",
        )
        
        mock_client = MagicMock()  # OpenMemory 客户端
        worker_id = "dedup-worker"
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            # 模拟 check_dedup 返回已存在的记录
            mock_adapter.check_dedup.return_value = {
                "outbox_id": 50,  # 原始成功的 outbox_id
                "target_space": "team:project",
                "payload_sha": "sha_already_sent",
                "status": "sent",
                "last_error": "memory_id=mem_original_123",
            }
            
            result = process_single_item(item, worker_id, mock_client, config)
            
            # 关键验证：OpenMemory store 不应被调用
            mock_client.store.assert_not_called()
            
            # 验证 ack_sent 被调用
            mock_adapter.ack_sent.assert_called_once_with(
                outbox_id=101,
                worker_id="dedup-worker",
                memory_id="mem_original_123"
            )
            
            # 验证结果
            assert result.success is True
            assert result.action == "allow"
            assert result.reason == "outbox_flush_dedup_hit"

    def test_dedup_hit_writes_audit(self, config):
        """dedup_hit 时写入审计日志"""
        item = make_outbox_item(
            outbox_id=102,
            target_space="private:alice",
            payload_sha="sha_dedup_audit",
        )
        
        mock_client = MagicMock()
        worker_id = "audit-dedup-worker"
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = {
                "outbox_id": 60,
                "target_space": "private:alice",
                "payload_sha": "sha_dedup_audit",
                "status": "sent",
                "last_error": "memory_id=mem_456",
            }
            
            process_single_item(item, worker_id, mock_client, config)
            
            # 验证审计日志写入
            mock_adapter.insert_write_audit.assert_called_once()
            call_kwargs = mock_adapter.insert_write_audit.call_args[1]
            
            assert call_kwargs["actor_user_id"] == "alice"
            assert call_kwargs["target_space"] == "private:alice"
            assert call_kwargs["action"] == "allow"
            assert call_kwargs["reason"] == "outbox_flush_dedup_hit"
            assert call_kwargs["payload_sha"] == "sha_dedup_audit"
            assert call_kwargs["evidence_refs_json"]["outbox_id"] == 102
            # original_outbox_id 位于 extra 子对象中（通过 build_evidence_refs_json 提升）
            assert call_kwargs["evidence_refs_json"]["extra"]["original_outbox_id"] == 60
            assert call_kwargs["evidence_refs_json"]["memory_id"] == "mem_456"

    def test_no_dedup_continues_normal_flow(self, config):
        """无重复记录时继续正常处理流程"""
        item = make_outbox_item(
            outbox_id=103,
            target_space="team:unique",
            payload_sha="sha_new_content",
        )
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(
            success=True,
            memory_id="mem_new_789",
        )
        worker_id = "normal-worker"
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            # 模拟 check_dedup 返回 None（无重复）
            mock_adapter.check_dedup.return_value = None
            
            result = process_single_item(item, worker_id, mock_client, config)
            
            # 验证 OpenMemory store 被调用
            mock_client.store.assert_called_once()
            
            # 验证结果是正常成功流程
            assert result.success is True
            assert result.action == "allow"
            assert result.reason == "outbox_flush_success"

    def test_dedup_extracts_memory_id_from_last_error(self, config):
        """正确从 last_error 提取 memory_id"""
        item = make_outbox_item(outbox_id=104, target_space="team:test")
        mock_client = MagicMock()
        worker_id = "extract-worker"
        
        test_cases = [
            ("memory_id=abc123", "abc123"),
            ("memory_id=", ""),
            (None, None),
            ("some_other_error", None),
        ]
        
        for last_error, expected_memory_id in test_cases:
            with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
                mock_adapter.check_dedup.return_value = {
                    "outbox_id": 70,
                    "target_space": "team:test",
                    "payload_sha": item.payload_sha,
                    "status": "sent",
                    "last_error": last_error,
                }
                
                process_single_item(item, worker_id, mock_client, config)
                
                # 验证传递给 ack_sent 的 memory_id
                call_kwargs = mock_adapter.ack_sent.call_args[1]
                assert call_kwargs["memory_id"] == expected_memory_id, \
                    f"last_error={last_error} 应提取 memory_id={expected_memory_id}"


# ======================== space 参数传递测试 ========================

class TestSpaceParameter:
    """测试 space 参数正确传递给 OpenMemory"""

    @pytest.fixture
    def config(self):
        return WorkerConfig(max_retries=3, jitter_factor=0.0)

    def test_space_passed_to_openmemory_private(self, config):
        """private 空间：验证 space 和 user_id 都正确传递"""
        item = make_outbox_item(
            outbox_id=1,
            target_space="private:alice",
            payload_sha="abc123",
        )
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(
            success=True,
            memory_id="mem_123",
        )
        
        worker_id = "test-worker"
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            result = process_single_item(item, worker_id, mock_client, config)
            
            # 验证 store 调用参数
            mock_client.store.assert_called_once()
            call_kwargs = mock_client.store.call_args[1]
            
            # 关键验证：space 参数正确传递
            assert call_kwargs["space"] == "private:alice"
            # 关键验证：user_id 从 private 空间提取
            assert call_kwargs["user_id"] == "alice"
            
            # 验证 metadata 包含必要字段
            metadata = call_kwargs["metadata"]
            assert metadata["outbox_id"] == 1
            assert metadata["payload_sha"] == "abc123"
            assert metadata["target_space"] == "private:alice"
            assert metadata["source"] == "outbox_worker"

    def test_space_passed_to_openmemory_team(self, config):
        """team 空间：验证 space 传递但 user_id 为 None"""
        item = make_outbox_item(
            outbox_id=2,
            target_space="team:project",
            payload_sha="def456",
        )
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(
            success=True,
            memory_id="mem_456",
        )
        
        worker_id = "test-worker"
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            result = process_single_item(item, worker_id, mock_client, config)
            
            call_kwargs = mock_client.store.call_args[1]
            
            # 关键验证：space 参数正确传递
            assert call_kwargs["space"] == "team:project"
            # team 空间不提取 user_id
            assert call_kwargs["user_id"] is None
            
            # 验证 metadata
            metadata = call_kwargs["metadata"]
            assert metadata["target_space"] == "team:project"


# ======================== claim/ack/fail 调用形态测试 ========================

class TestLeaseProtocolCalls:
    """测试 Lease 协议的调用形态"""

    @pytest.fixture
    def config(self):
        return WorkerConfig(batch_size=5, max_retries=3, lease_seconds=120)

    def test_claim_outbox_call_shape(self, config):
        """验证 claim_outbox 调用包含 worker_id, limit, lease_seconds"""
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter, \
             patch("engram.gateway.outbox_worker.openmemory_client"):
            mock_adapter.claim_outbox.return_value = []
            
            process_batch(config, worker_id="my-worker-123")
            
            # 验证 claim_outbox 调用形态
            mock_adapter.claim_outbox.assert_called_once_with(
                worker_id="my-worker-123",
                limit=5,
                lease_seconds=120
            )

    def test_ack_sent_call_on_success(self, config):
        """成功时验证 ack_sent 调用形态"""
        item = make_outbox_item(outbox_id=10, target_space="private:bob")
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(
            success=True,
            memory_id="mem_success",
        )
        
        worker_id = "worker-abc"
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            process_single_item(item, worker_id, mock_client, config)
            
            # 验证 ack_sent 调用形态
            mock_adapter.ack_sent.assert_called_once_with(
                outbox_id=10,
                worker_id="worker-abc",
                memory_id="mem_success"
            )

    def test_fail_retry_call_on_failure(self, config):
        """失败时验证 fail_retry 调用形态（含 next_attempt_at）"""
        # 使用 jitter_factor=0.0 以便确定性验证
        config_no_jitter = WorkerConfig(batch_size=5, max_retries=3, lease_seconds=120, jitter_factor=0.0)
        
        item = make_outbox_item(outbox_id=20, target_space="private:carol", retry_count=0)
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(
            success=False,
            error="connection_timeout",
        )
        
        worker_id = "worker-xyz"
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            before_time = datetime.now(timezone.utc)
            process_single_item(item, worker_id, mock_client, config_no_jitter)
            after_time = datetime.now(timezone.utc)
            
            # 验证 fail_retry 调用形态
            mock_adapter.fail_retry.assert_called_once()
            call_kwargs = mock_adapter.fail_retry.call_args[1]
            
            assert call_kwargs["outbox_id"] == 20
            assert call_kwargs["worker_id"] == "worker-xyz"
            assert call_kwargs["error"] == "connection_timeout"
            
            # 验证 next_attempt_at 是 datetime 且在合理范围内
            next_attempt_at = call_kwargs["next_attempt_at"]
            assert isinstance(next_attempt_at, datetime)
            
            # new_retry_count=1, base=60, jitter=0.0 -> 预期退避 60*2^1 = 120 秒
            expected_backoff = 120
            expected_min = before_time + timedelta(seconds=expected_backoff - 1)
            expected_max = after_time + timedelta(seconds=expected_backoff + 1)
            assert expected_min <= next_attempt_at <= expected_max, \
                f"next_attempt_at={next_attempt_at} 应在预期范围内"
            
            # 验证未调用 ack_sent 或 mark_dead
            mock_adapter.ack_sent.assert_not_called()
            mock_adapter.mark_dead.assert_not_called()

    def test_fail_retry_next_attempt_at_deterministic(self):
        """验证同一 retry_count 输入产生可预期的 next_attempt_at（jitter=0）"""
        # 使用 jitter_factor=0.0 以便确定性验证
        config_no_jitter = WorkerConfig(batch_size=5, max_retries=5, lease_seconds=120, jitter_factor=0.0)
        
        # 测试不同 retry_count 产生的 next_attempt_at
        test_cases = [
            # (retry_count, expected_backoff_seconds)
            (0, 120),   # new_retry_count=1, base=60, 60*2^1=120
            (1, 240),   # new_retry_count=2, base=60, 60*2^2=240
            (2, 480),   # new_retry_count=3, base=60, 60*2^3=480
        ]
        
        for retry_count, expected_backoff in test_cases:
            item = make_outbox_item(
                outbox_id=100 + retry_count,
                target_space="private:test",
                retry_count=retry_count
            )
            
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(
                success=False,
                error="test_error",
            )
            
            with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
                mock_adapter.check_dedup.return_value = None  # 非重复记录
                before_time = datetime.now(timezone.utc)
                process_single_item(item, "test-worker", mock_client, config_no_jitter)
                after_time = datetime.now(timezone.utc)
                
                call_kwargs = mock_adapter.fail_retry.call_args[1]
                next_attempt_at = call_kwargs["next_attempt_at"]
                
                # 验证 next_attempt_at 在预期范围内
                expected_min = before_time + timedelta(seconds=expected_backoff - 1)
                expected_max = after_time + timedelta(seconds=expected_backoff + 1)
                
                assert expected_min <= next_attempt_at <= expected_max, \
                    f"retry_count={retry_count}: next_attempt_at={next_attempt_at} 应约为 now+{expected_backoff}s"

    def test_mark_dead_call_on_max_retries(self, config):
        """超过最大重试时验证 mark_dead 调用形态"""
        # retry_count=2, max_retries=3, 再失败就是第3次 >= max_retries
        item = make_outbox_item(outbox_id=30, target_space="private:dave", retry_count=2)
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(
            success=False,
            error="permanent_failure",
        )
        
        worker_id = "worker-dead"
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            result = process_single_item(item, worker_id, mock_client, config)
            
            # 验证 mark_dead 调用形态
            mock_adapter.mark_dead.assert_called_once_with(
                outbox_id=30,
                worker_id="worker-dead",
                error="permanent_failure"
            )
            
            # 验证结果
            assert result.action == "reject"
            assert result.reason == "outbox_flush_dead"


# ======================== 处理结果测试 ========================

class TestProcessResults:
    """处理结果测试"""

    @pytest.fixture
    def config(self):
        return WorkerConfig(max_retries=3, jitter_factor=0.0)

    def test_success_result(self, config):
        """成功时返回正确结果"""
        item = make_outbox_item(outbox_id=1)
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(success=True, memory_id="mem_1")
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            result = process_single_item(item, "worker", mock_client, config)
            
            assert result.success is True
            assert result.action == "allow"
            assert result.reason == "outbox_flush_success"
            assert result.error is None

    def test_retry_result(self, config):
        """重试时返回正确结果"""
        item = make_outbox_item(outbox_id=2, retry_count=0)
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(success=False, error="temp_error")
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            result = process_single_item(item, "worker", mock_client, config)
            
            assert result.success is False
            assert result.action == "redirect"
            assert result.reason == "outbox_flush_retry"
            assert result.error == "temp_error"

    def test_dead_result(self, config):
        """死信时返回正确结果"""
        item = make_outbox_item(outbox_id=3, retry_count=2)  # 下次就是第3次
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(success=False, error="perm_error")
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            result = process_single_item(item, "worker", mock_client, config)
            
            assert result.success is False
            assert result.action == "reject"
            assert result.reason == "outbox_flush_dead"
            assert result.error == "perm_error"


# ======================== 批量处理测试 ========================

class TestOpenMemoryClientConfig:
    """验证 OpenMemory 客户端配置传递"""

    def test_process_batch_creates_client_with_configured_timeout_and_retries(self):
        """验证 process_batch 使用 WorkerConfig 中的 timeout 和 max_retries 创建客户端"""
        from gateway import openmemory_client
        
        config = WorkerConfig(
            batch_size=5,
            max_retries=3,
            openmemory_timeout_seconds=15.0,
            openmemory_max_client_retries=1,
        )
        
        captured_client_args = {}
        
        original_init = openmemory_client.OpenMemoryClient.__init__
        
        def capture_init(self, *args, **kwargs):
            captured_client_args.update(kwargs)
            original_init(self, *args, **kwargs)
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter, \
             patch.object(openmemory_client.OpenMemoryClient, "__init__", capture_init):
            mock_adapter.claim_outbox.return_value = [{
                "outbox_id": 1,
                "item_id": None,
                "target_space": "private:test",
                "payload_md": "test",
                "payload_sha": "sha123",
                "status": "pending",
                "retry_count": 0,
            }]
            mock_adapter.OutboxItem = OutboxItem
            mock_adapter.check_dedup.return_value = None
            
            # 模拟 store 方法
            with patch.object(openmemory_client.OpenMemoryClient, "store", return_value=MockStoreResult(success=True, memory_id="mem_1")):
                process_batch(config, worker_id="test-worker")
        
        # 验证 timeout 和 retry_config 被正确传递
        assert captured_client_args.get("timeout") == 15.0, \
            f"timeout 应为 15.0，实际: {captured_client_args.get('timeout')}"
        
        retry_config = captured_client_args.get("retry_config")
        assert retry_config is not None, "retry_config 应被传递"
        assert retry_config.max_retries == 1, f"max_retries 应为 1，实际: {retry_config.max_retries}"

    def test_default_openmemory_client_config_no_retries(self):
        """验证默认配置下 OpenMemory 客户端不进行内部重试"""
        from gateway import openmemory_client
        
        config = WorkerConfig()  # 使用默认值
        
        captured_client_args = {}
        
        original_init = openmemory_client.OpenMemoryClient.__init__
        
        def capture_init(self, *args, **kwargs):
            captured_client_args.update(kwargs)
            original_init(self, *args, **kwargs)
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter, \
             patch.object(openmemory_client.OpenMemoryClient, "__init__", capture_init):
            mock_adapter.claim_outbox.return_value = []
            
            process_batch(config, worker_id="test-worker")
        
        # 即使没有任务，客户端也不会被创建，所以这里检查空记录情况
        # 需要有任务才会创建客户端
        # 重新测试有任务的情况
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter, \
             patch.object(openmemory_client.OpenMemoryClient, "__init__", capture_init):
            mock_adapter.claim_outbox.return_value = [{
                "outbox_id": 1,
                "item_id": None,
                "target_space": "private:test",
                "payload_md": "test",
                "payload_sha": "sha123",
                "status": "pending",
                "retry_count": 0,
            }]
            mock_adapter.OutboxItem = OutboxItem
            mock_adapter.check_dedup.return_value = None
            
            with patch.object(openmemory_client.OpenMemoryClient, "store", return_value=MockStoreResult(success=True, memory_id="mem_1")):
                process_batch(config, worker_id="test-worker")
        
        # 验证默认值
        assert captured_client_args.get("timeout") == 30.0, \
            f"默认 timeout 应为 30.0，实际: {captured_client_args.get('timeout')}"
        
        retry_config = captured_client_args.get("retry_config")
        assert retry_config is not None
        assert retry_config.max_retries == 0, f"默认 max_retries 应为 0，实际: {retry_config.max_retries}"


class TestProcessBatch:
    """批量处理测试"""

    @pytest.fixture
    def config(self):
        return WorkerConfig(batch_size=5, max_retries=3, lease_seconds=60)

    def test_empty_batch(self, config):
        """无待处理记录时返回空列表"""
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.claim_outbox.return_value = []
            
            results = process_batch(config, worker_id="test-worker")
            
            assert results == []

    def test_batch_converts_dicts_to_items(self, config):
        """批量处理正确转换字典为 OutboxItem"""
        raw_items = [
            {
                "outbox_id": i,
                "item_id": None,
                "target_space": f"private:user_{i}",
                "payload_md": f"Content {i}",
                "payload_sha": f"sha_{i}",
                "status": "pending",
                "retry_count": 0,
            }
            for i in range(3)
        ]
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter, \
             patch("engram.gateway.outbox_worker.openmemory_client") as mock_om:
            mock_adapter.claim_outbox.return_value = raw_items
            mock_adapter.OutboxItem = OutboxItem
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            
            mock_client = MagicMock()
            mock_client.store.return_value = MockStoreResult(success=True, memory_id="mem_ok")
            mock_om.OpenMemoryClient.return_value = mock_client
            
            results = process_batch(config, worker_id="batch-worker")
            
            assert len(results) == 3
            assert all(r.success for r in results)
            
            # 验证 ack_sent 被调用 3 次
            assert mock_adapter.ack_sent.call_count == 3


# ======================== 审计日志测试 ========================

class TestAuditLogging:
    """审计日志测试"""

    @pytest.fixture
    def config(self):
        return WorkerConfig(max_retries=3)

    def test_success_writes_audit(self, config):
        """成功时写入审计日志"""
        item = make_outbox_item(
            outbox_id=100,
            target_space="private:audit_user",
            payload_sha="audit_sha",
        )
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(success=True, memory_id="audit_mem")
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            process_single_item(item, "audit-worker", mock_client, config)
            
            # 验证 insert_write_audit 调用
            mock_adapter.insert_write_audit.assert_called_once()
            call_kwargs = mock_adapter.insert_write_audit.call_args[1]
            
            assert call_kwargs["actor_user_id"] == "audit_user"
            assert call_kwargs["target_space"] == "private:audit_user"
            assert call_kwargs["action"] == "allow"
            assert call_kwargs["reason"] == "outbox_flush_success"
            assert call_kwargs["payload_sha"] == "audit_sha"
            assert call_kwargs["evidence_refs_json"]["outbox_id"] == 100
            assert call_kwargs["evidence_refs_json"]["memory_id"] == "audit_mem"


# ======================== 审计 action/reason 前缀与 evidence_refs_json 验证测试 ========================

class TestAuditFieldValidation:
    """验证审计记录的 action/reason 前缀与 evidence_refs_json 关键字段"""

    @pytest.fixture
    def config(self):
        return WorkerConfig(max_retries=3, jitter_factor=0.0)

    def test_success_audit_reason_prefix_and_evidence_keys(self, config):
        """成功：验证 reason 使用 outbox_flush_ 前缀，evidence_refs_json 包含关键字段"""
        item = make_outbox_item(
            outbox_id=200,
            target_space="private:test_user",
            payload_sha="sha_success_123",
        )
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(success=True, memory_id="mem_200")
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            result = process_single_item(item, "worker-test", mock_client, config)
            
            # 验证结果
            assert result.success is True
            assert result.action == "allow"
            
            # 验证审计调用
            mock_adapter.insert_write_audit.assert_called_once()
            call_kwargs = mock_adapter.insert_write_audit.call_args[1]
            
            # 验证 action 是有效值
            assert call_kwargs["action"] in ("allow", "redirect", "reject")
            
            # 验证 reason 使用 outbox_flush_ 前缀
            assert call_kwargs["reason"].startswith("outbox_flush_"), \
                f"reason 应以 'outbox_flush_' 开头，实际: {call_kwargs['reason']}"
            
            # 验证 evidence_refs_json 包含关键字段
            evidence = call_kwargs["evidence_refs_json"]
            assert "outbox_id" in evidence, "evidence_refs_json 应包含 outbox_id"
            assert "memory_id" in evidence, "evidence_refs_json 应包含 memory_id"
            assert "payload_sha" in evidence, "evidence_refs_json 应包含 payload_sha"
            assert "source" in evidence, "evidence_refs_json 应包含 source"
            
            # 验证字段值
            assert evidence["outbox_id"] == 200
            assert evidence["memory_id"] == "mem_200"
            assert evidence["payload_sha"] == "sha_success_123"
            assert evidence["source"] == "outbox_worker"

    def test_retry_audit_reason_prefix_and_evidence_keys(self, config):
        """重试：验证 reason 使用 outbox_flush_retry，evidence_refs_json 包含关键字段"""
        item = make_outbox_item(
            outbox_id=201,
            target_space="private:retry_user",
            payload_sha="sha_retry_456",
            retry_count=0,
        )
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(success=False, error="connection_failed")
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            result = process_single_item(item, "worker-test", mock_client, config)
            
            # 验证结果
            assert result.success is False
            assert result.action == "redirect"
            assert result.reason == "outbox_flush_retry"
            
            # 验证审计调用
            mock_adapter.insert_write_audit.assert_called_once()
            call_kwargs = mock_adapter.insert_write_audit.call_args[1]
            
            # 验证 action
            assert call_kwargs["action"] == "redirect"
            
            # 验证 reason 前缀
            assert call_kwargs["reason"] == "outbox_flush_retry"
            
            # 验证 evidence_refs_json 关键字段
            evidence = call_kwargs["evidence_refs_json"]
            assert "outbox_id" in evidence
            assert "retry_count" in evidence
            assert "next_attempt_at" in evidence
            assert "payload_sha" in evidence
            assert "source" in evidence
            assert "extra" in evidence
            assert "last_error" in evidence["extra"]  # last_error 在 extra 中
            
            # 验证字段值
            assert evidence["outbox_id"] == 201
            assert evidence["retry_count"] == 1  # retry_count + 1
            assert evidence["extra"]["last_error"] == "connection_failed"
            assert evidence["payload_sha"] == "sha_retry_456"
            assert evidence["source"] == "outbox_worker"

    def test_dead_audit_reason_prefix_and_evidence_keys(self, config):
        """死信：验证 reason 使用 outbox_flush_dead，evidence_refs_json 包含关键字段"""
        item = make_outbox_item(
            outbox_id=202,
            target_space="private:dead_user",
            payload_sha="sha_dead_789",
            retry_count=2,  # 下次就是第3次 >= max_retries
        )
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(success=False, error="permanent_error")
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None  # 非重复记录
            result = process_single_item(item, "worker-test", mock_client, config)
            
            # 验证结果
            assert result.success is False
            assert result.action == "reject"
            assert result.reason == "outbox_flush_dead"
            
            # 验证审计调用
            mock_adapter.insert_write_audit.assert_called_once()
            call_kwargs = mock_adapter.insert_write_audit.call_args[1]
            
            # 验证 action
            assert call_kwargs["action"] == "reject"
            
            # 验证 reason 前缀
            assert call_kwargs["reason"] == "outbox_flush_dead"
            
            # 验证 evidence_refs_json 关键字段
            evidence = call_kwargs["evidence_refs_json"]
            assert "outbox_id" in evidence
            assert "retry_count" in evidence
            assert "payload_sha" in evidence
            assert "source" in evidence
            assert "extra" in evidence
            assert "last_error" in evidence["extra"]  # last_error 在 extra 中
            
            # 验证字段值
            assert evidence["outbox_id"] == 202
            assert evidence["retry_count"] == 3  # retry_count + 1 = 3
            assert evidence["extra"]["last_error"] == "permanent_error"
            assert evidence["payload_sha"] == "sha_dead_789"
            assert evidence["source"] == "outbox_worker"

    def test_team_space_audit_evidence_keys(self, config):
        """team 空间：验证 evidence_refs_json 仍包含所有关键字段"""
        item = make_outbox_item(
            outbox_id=203,
            target_space="team:project_abc",
            payload_sha="sha_team_000",
        )
        
        mock_client = MagicMock()
        mock_client.store.return_value = MockStoreResult(success=True, memory_id="mem_team")
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            result = process_single_item(item, "worker-test", mock_client, config)
            
            # 验证审计调用
            mock_adapter.insert_write_audit.assert_called_once()
            call_kwargs = mock_adapter.insert_write_audit.call_args[1]
            
            # team 空间的 actor_user_id 应为 None
            assert call_kwargs["actor_user_id"] is None
            assert call_kwargs["target_space"] == "team:project_abc"
            
            # 验证 evidence_refs_json 关键字段仍完整
            evidence = call_kwargs["evidence_refs_json"]
            assert "outbox_id" in evidence
            assert "memory_id" in evidence
            assert "payload_sha" in evidence
            assert "source" in evidence
            
            assert evidence["payload_sha"] == "sha_team_000"
            assert evidence["source"] == "outbox_worker"


# ======================== WorkerConfig 测试 ========================

class TestWorkerConfig:
    """WorkerConfig 配置测试"""

    def test_default_config(self):
        """默认配置值"""
        config = WorkerConfig()
        assert config.batch_size == 10
        assert config.max_retries == 5
        assert config.base_backoff_seconds == 60
        assert config.max_backoff_seconds == 3600
        assert config.jitter_factor == 0.3
        assert config.loop_interval == 5.0
        assert config.lease_seconds == 120
        # 新增的 OpenMemory 客户端配置字段
        assert config.openmemory_timeout_seconds == 30.0
        assert config.openmemory_max_client_retries == 0

    def test_custom_config(self):
        """自定义配置"""
        config = WorkerConfig(
            batch_size=20,
            max_retries=10,
            lease_seconds=300,
        )
        assert config.batch_size == 20
        assert config.max_retries == 10
        assert config.lease_seconds == 300

    def test_custom_openmemory_config(self):
        """自定义 OpenMemory 客户端配置"""
        config = WorkerConfig(
            openmemory_timeout_seconds=15.0,
            openmemory_max_client_retries=1,
        )
        assert config.openmemory_timeout_seconds == 15.0
        assert config.openmemory_max_client_retries == 1


# ======================== run_once 测试 ========================

class TestRunOnce:
    """run_once 模式测试"""

    def test_run_once_returns_results(self):
        """run_once 返回处理结果"""
        config = WorkerConfig(batch_size=5)
        
        with patch("engram.gateway.outbox_worker.process_batch") as mock_batch:
            mock_batch.return_value = [
                ProcessResult(outbox_id=1, success=True, action="allow", reason="ok"),
                ProcessResult(outbox_id=2, success=False, action="redirect", reason="retry"),
            ]
            
            results = run_once(config, worker_id="test")
            
            assert len(results) == 2
            assert results[0].success is True
            assert results[1].success is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
