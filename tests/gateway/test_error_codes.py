# -*- coding: utf-8 -*-
"""
错误码规范化测试

测试覆盖:
- ErrorCode 常量定义验证
- Gateway main.py 错误码使用验证
- Outbox Worker 错误码使用验证
- 关键路径测试断言（例如 OpenMemory 连接失败 -> reason 必须为 openmemory_write_failed:connection_error）
"""

import pytest
import sys
import os
from unittest.mock import MagicMock, patch, AsyncMock
from dataclasses import dataclass
from typing import Optional

# 确保 engram_logbook 可导入
logbook_scripts_path = os.path.join(
    os.path.dirname(__file__), 
    "..", "..", "..", "..", 
    "logbook_postgres", "scripts"
)
if logbook_scripts_path not in sys.path:
    sys.path.insert(0, os.path.abspath(logbook_scripts_path))

from engram.logbook.errors import ErrorCode


# ======================== ErrorCode 常量验证测试 ========================

class TestErrorCodeConstants:
    """验证 ErrorCode 常量定义"""

    def test_openmemory_error_codes_exist(self):
        """验证 OpenMemory 相关错误码存在"""
        assert hasattr(ErrorCode, 'OPENMEMORY_WRITE_FAILED_CONNECTION')
        assert hasattr(ErrorCode, 'OPENMEMORY_WRITE_FAILED_API')
        assert hasattr(ErrorCode, 'OPENMEMORY_WRITE_FAILED_GENERIC')
        assert hasattr(ErrorCode, 'OPENMEMORY_WRITE_FAILED_UNKNOWN')

    def test_outbox_error_codes_exist(self):
        """验证 Outbox Worker 相关错误码存在"""
        assert hasattr(ErrorCode, 'OUTBOX_FLUSH_SUCCESS')
        assert hasattr(ErrorCode, 'OUTBOX_FLUSH_RETRY')
        assert hasattr(ErrorCode, 'OUTBOX_FLUSH_DEAD')
        assert hasattr(ErrorCode, 'OUTBOX_FLUSH_CONFLICT')
        assert hasattr(ErrorCode, 'OUTBOX_FLUSH_DEDUP_HIT')
        assert hasattr(ErrorCode, 'OUTBOX_FLUSH_DB_TIMEOUT')
        assert hasattr(ErrorCode, 'OUTBOX_FLUSH_DB_ERROR')

    def test_actor_error_codes_exist(self):
        """验证 Actor 用户相关错误码存在"""
        assert hasattr(ErrorCode, 'ACTOR_UNKNOWN_REJECT')
        assert hasattr(ErrorCode, 'ACTOR_UNKNOWN_DEGRADE')
        assert hasattr(ErrorCode, 'ACTOR_AUTOCREATED')
        assert hasattr(ErrorCode, 'ACTOR_AUTOCREATE_FAILED')

    def test_governance_error_codes_exist(self):
        """验证治理相关错误码存在"""
        assert hasattr(ErrorCode, 'GOVERNANCE_UPDATE_MISSING_CREDENTIALS')
        assert hasattr(ErrorCode, 'GOVERNANCE_UPDATE_INVALID_ADMIN_KEY')
        assert hasattr(ErrorCode, 'GOVERNANCE_UPDATE_USER_NOT_IN_ALLOWLIST')
        assert hasattr(ErrorCode, 'GOVERNANCE_UPDATE_INTERNAL_ERROR')

    def test_error_code_values_follow_convention(self):
        """验证错误码值遵循命名规范"""
        # OpenMemory 错误码格式: openmemory_write_failed:<detail>
        assert ErrorCode.OPENMEMORY_WRITE_FAILED_CONNECTION == "openmemory_write_failed:connection_error"
        assert ErrorCode.OPENMEMORY_WRITE_FAILED_API == "openmemory_write_failed:api_error"
        assert ErrorCode.OPENMEMORY_WRITE_FAILED_GENERIC == "openmemory_write_failed:openmemory_error"
        
        # Outbox 错误码格式: outbox_flush_<status>
        assert ErrorCode.OUTBOX_FLUSH_SUCCESS == "outbox_flush_success"
        assert ErrorCode.OUTBOX_FLUSH_RETRY == "outbox_flush_retry"
        assert ErrorCode.OUTBOX_FLUSH_DEAD == "outbox_flush_dead"
        assert ErrorCode.OUTBOX_FLUSH_CONFLICT == "outbox_flush_conflict"
        assert ErrorCode.OUTBOX_FLUSH_DEDUP_HIT == "outbox_flush_dedup_hit"
        assert ErrorCode.OUTBOX_FLUSH_DB_TIMEOUT == "outbox_flush_db_timeout"
        assert ErrorCode.OUTBOX_FLUSH_DB_ERROR == "outbox_flush_db_error"
        
        # Actor 错误码格式: actor_<status>:<detail>
        assert ErrorCode.ACTOR_UNKNOWN_REJECT == "actor_unknown:reject"
        assert ErrorCode.ACTOR_UNKNOWN_DEGRADE == "actor_unknown:degrade"

    def test_openmemory_api_error_method(self):
        """验证 openmemory_api_error 方法"""
        # 无状态码
        assert ErrorCode.openmemory_api_error() == "openmemory_write_failed:api_error"
        # 有状态码
        assert ErrorCode.openmemory_api_error(404) == "openmemory_write_failed:api_error_404"
        assert ErrorCode.openmemory_api_error(500) == "openmemory_write_failed:api_error_500"

    def test_policy_reason_method(self):
        """验证 policy_reason 方法"""
        assert ErrorCode.policy_reason("team_write_disabled") == "policy:team_write_disabled"
        assert ErrorCode.policy_reason("bulk_rejected") == "policy:bulk_rejected"


# ======================== Gateway 关键路径错误码测试 ========================

class TestGatewayErrorCodes:
    """验证 Gateway main.py 中关键路径的错误码使用"""

    @pytest.fixture
    def mock_config(self):
        """模拟配置"""
        config = MagicMock()
        config.default_team_space = "team:test"
        config.project_key = "test_project"
        config.private_space_prefix = "private:"
        config.governance_admin_key = "test_admin_key"
        return config

    @pytest.mark.asyncio
    async def test_openmemory_connection_error_reason(self, mock_config):
        """关键路径测试: OpenMemory 连接失败 -> reason 必须为 openmemory_write_failed:connection_error"""
        from engram.gateway.main import memory_store_impl
        from engram.gateway.openmemory_client import OpenMemoryConnectionError
        
        # 捕获的审计调用
        captured_audits = []
        
        def capture_audit(**kwargs):
            captured_audits.append(kwargs)
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.get_client") as mock_get_client, \
             patch("engram.gateway.main.logbook_adapter") as mock_logbook_adapter, \
             patch("engram.gateway.main.create_engine_from_settings") as mock_create_engine:
            
            mock_get_config.return_value = mock_config
            
            # 模拟 DB
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {"team_write_enabled": True}
            mock_db.insert_audit = capture_audit
            mock_db.enqueue_outbox.return_value = 123
            mock_get_db.return_value = mock_db
            
            # 模拟策略引擎返回 allow
            mock_decision = MagicMock()
            mock_decision.action.value = "allow"
            mock_decision.reason = "team_write_enabled"
            mock_decision.final_space = "team:test"
            mock_engine = MagicMock()
            mock_engine.decide.return_value = mock_decision
            mock_create_engine.return_value = mock_engine
            
            # 模拟 dedup check 返回 None（无重复）
            mock_logbook_adapter.check_dedup.return_value = None
            
            # 模拟 OpenMemory 连接失败
            mock_client = MagicMock()
            mock_client.store.side_effect = OpenMemoryConnectionError("Connection refused")
            mock_get_client.return_value = mock_client
            
            # 执行
            result = await memory_store_impl(
                payload_md="test content",
                target_space="team:test",
            )
            
            # 验证结果 - 使用统一响应契约
            assert result.ok is False
            assert result.action == "deferred", \
                f"OpenMemory 失败时 action 必须为 'deferred'（已入队 outbox），实际: {result.action}"
            
            # 关键断言：验证 deferred 响应必须包含 outbox_id
            assert result.outbox_id == 123, \
                f"deferred 响应必须包含 outbox_id，实际: {result.outbox_id}"
            
            # 关键断言：验证响应必须包含 correlation_id
            assert result.correlation_id is not None, \
                f"响应必须包含 correlation_id，实际: {result.correlation_id}"
            assert result.correlation_id.startswith("corr-"), \
                f"correlation_id 必须以 'corr-' 开头，实际: {result.correlation_id}"
            
            # 关键断言：验证审计 reason 为 openmemory_write_failed:connection_error
            # 注意：现在有2条审计（预审计 + 失败审计）
            assert len(captured_audits) >= 1
            # 找到失败审计（reason 包含 openmemory_write_failed）
            failure_audit = None
            for audit in captured_audits:
                if "openmemory_write_failed" in str(audit.get("reason", "")):
                    failure_audit = audit
                    break
            assert failure_audit is not None, \
                f"应存在 openmemory_write_failed 审计记录，实际审计: {captured_audits}"
            assert failure_audit["reason"] == ErrorCode.OPENMEMORY_WRITE_FAILED_CONNECTION, \
                f"OpenMemory 连接失败时 reason 必须为 {ErrorCode.OPENMEMORY_WRITE_FAILED_CONNECTION}，实际: {failure_audit['reason']}"

    @pytest.mark.asyncio
    async def test_actor_unknown_reject_reason(self, mock_config):
        """关键路径测试: Actor 不存在且策略为 reject -> reason 必须为 actor_unknown:reject"""
        from engram.gateway.main import _validate_actor_user
        from engram.gateway.logbook_adapter import UnknownActorPolicy
        
        # 配置为 reject 策略
        mock_config.unknown_actor_policy = UnknownActorPolicy.REJECT
        
        captured_audits = []
        
        def capture_audit(**kwargs):
            captured_audits.append(kwargs)
        
        with patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.check_user_exists") as mock_check_user:
            
            mock_db = MagicMock()
            mock_db.insert_audit = capture_audit
            mock_get_db.return_value = mock_db
            
            # 模拟用户不存在
            mock_check_user.return_value = False
            
            # 执行
            result = _validate_actor_user(
                actor_user_id="unknown_user",
                config=mock_config,
                target_space="team:test",
                payload_sha="test_sha",
                evidence_refs=None,
                correlation_id="test_corr",
            )
            
            # 验证返回拒绝响应
            assert result is not None
            assert result.action == "reject"
            
            # 关键断言：验证审计 reason
            assert len(captured_audits) == 1
            audit = captured_audits[0]
            assert audit["reason"] == ErrorCode.ACTOR_UNKNOWN_REJECT, \
                f"Actor 不存在时 reason 必须为 {ErrorCode.ACTOR_UNKNOWN_REJECT}，实际: {audit['reason']}"


# ======================== Outbox Worker 关键路径错误码测试 ========================

class TestOutboxWorkerErrorCodes:
    """验证 Outbox Worker 中关键路径的错误码使用"""

    @pytest.fixture
    def config(self):
        from engram.gateway.outbox_worker import WorkerConfig
        return WorkerConfig(max_retries=3, jitter_factor=0.0)

    @dataclass
    class MockStoreResult:
        success: bool
        memory_id: Optional[str] = None
        error: Optional[str] = None

    def make_outbox_item(
        self,
        outbox_id: int = 1,
        target_space: str = "private:user_001",
        payload_md: str = "# Test Memory",
        payload_sha: str = "a" * 64,
        retry_count: int = 0,
    ):
        from engram.gateway.logbook_adapter import OutboxItem
        return OutboxItem(
            outbox_id=outbox_id,
            item_id=None,
            target_space=target_space,
            payload_md=payload_md,
            payload_sha=payload_sha,
            retry_count=retry_count,
        )

    def test_outbox_flush_success_reason(self, config):
        """关键路径测试: Outbox 写入成功 -> reason 必须为 outbox_flush_success"""
        from engram.gateway.outbox_worker import process_single_item
        
        item = self.make_outbox_item(outbox_id=1)
        
        mock_client = MagicMock()
        mock_client.store.return_value = self.MockStoreResult(success=True, memory_id="mem_123")
        
        captured_audits = []
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None
            mock_adapter.insert_write_audit = lambda **kwargs: captured_audits.append(kwargs)
            
            result = process_single_item(item, "worker-test", mock_client, config)
            
            # 验证结果
            assert result.success is True
            assert result.action == "allow"
            
            # 关键断言：验证 reason
            assert result.reason == ErrorCode.OUTBOX_FLUSH_SUCCESS, \
                f"Outbox 写入成功时 reason 必须为 {ErrorCode.OUTBOX_FLUSH_SUCCESS}，实际: {result.reason}"
            
            # 验证审计记录
            assert len(captured_audits) == 1
            assert captured_audits[0]["reason"] == ErrorCode.OUTBOX_FLUSH_SUCCESS

    def test_outbox_flush_retry_reason(self, config):
        """关键路径测试: Outbox 写入失败需重试 -> reason 必须为 outbox_flush_retry"""
        from engram.gateway.outbox_worker import process_single_item
        
        item = self.make_outbox_item(outbox_id=2, retry_count=0)
        
        mock_client = MagicMock()
        mock_client.store.return_value = self.MockStoreResult(success=False, error="temp_error")
        
        captured_audits = []
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None
            mock_adapter.insert_write_audit = lambda **kwargs: captured_audits.append(kwargs)
            
            result = process_single_item(item, "worker-test", mock_client, config)
            
            # 验证结果
            assert result.success is False
            assert result.action == "redirect"
            
            # 关键断言：验证 reason
            assert result.reason == ErrorCode.OUTBOX_FLUSH_RETRY, \
                f"Outbox 重试时 reason 必须为 {ErrorCode.OUTBOX_FLUSH_RETRY}，实际: {result.reason}"
            
            # 验证审计记录
            assert len(captured_audits) == 1
            assert captured_audits[0]["reason"] == ErrorCode.OUTBOX_FLUSH_RETRY

    def test_outbox_flush_dead_reason(self, config):
        """关键路径测试: Outbox 超过最大重试 -> reason 必须为 outbox_flush_dead"""
        from engram.gateway.outbox_worker import process_single_item
        
        item = self.make_outbox_item(outbox_id=3, retry_count=2)  # 下次就是第3次 >= max_retries
        
        mock_client = MagicMock()
        mock_client.store.return_value = self.MockStoreResult(success=False, error="perm_error")
        
        captured_audits = []
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None
            mock_adapter.insert_write_audit = lambda **kwargs: captured_audits.append(kwargs)
            
            result = process_single_item(item, "worker-test", mock_client, config)
            
            # 验证结果
            assert result.success is False
            assert result.action == "reject"
            
            # 关键断言：验证 reason
            assert result.reason == ErrorCode.OUTBOX_FLUSH_DEAD, \
                f"Outbox 死信时 reason 必须为 {ErrorCode.OUTBOX_FLUSH_DEAD}，实际: {result.reason}"
            
            # 验证审计记录
            assert len(captured_audits) == 1
            assert captured_audits[0]["reason"] == ErrorCode.OUTBOX_FLUSH_DEAD

    def test_outbox_flush_dedup_hit_reason(self, config):
        """关键路径测试: Outbox 去重命中 -> reason 必须为 outbox_flush_dedup_hit"""
        from engram.gateway.outbox_worker import process_single_item
        
        item = self.make_outbox_item(outbox_id=4)
        
        mock_client = MagicMock()  # 不应被调用
        
        captured_audits = []
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = {
                "outbox_id": 100,
                "status": "sent",
                "last_error": "memory_id=mem_original",
            }
            mock_adapter.insert_write_audit = lambda **kwargs: captured_audits.append(kwargs)
            
            result = process_single_item(item, "worker-test", mock_client, config)
            
            # 验证结果
            assert result.success is True
            assert result.action == "allow"
            
            # 关键断言：验证 reason
            assert result.reason == ErrorCode.OUTBOX_FLUSH_DEDUP_HIT, \
                f"Outbox 去重命中时 reason 必须为 {ErrorCode.OUTBOX_FLUSH_DEDUP_HIT}，实际: {result.reason}"
            
            # 验证 OpenMemory 未被调用
            mock_client.store.assert_not_called()

    def test_outbox_connection_error_triggers_retry(self, config):
        """关键路径测试: OpenMemory 连接错误应触发重试而非死信"""
        from engram.gateway.outbox_worker import process_single_item
        from engram.gateway.openmemory_client import OpenMemoryConnectionError
        
        item = self.make_outbox_item(outbox_id=5, retry_count=0)
        
        mock_client = MagicMock()
        mock_client.store.side_effect = OpenMemoryConnectionError("Connection refused")
        
        captured_audits = []
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            mock_adapter.check_dedup.return_value = None
            mock_adapter.insert_write_audit = lambda **kwargs: captured_audits.append(kwargs)
            
            result = process_single_item(item, "worker-test", mock_client, config)
            
            # 验证结果：应该是重试而非死信
            assert result.success is False
            assert result.action == "redirect"
            
            # 关键断言：验证 reason 为 retry
            assert result.reason == ErrorCode.OUTBOX_FLUSH_RETRY, \
                f"连接错误应触发重试，reason 必须为 {ErrorCode.OUTBOX_FLUSH_RETRY}，实际: {result.reason}"


# ======================== 错误码一致性测试 ========================

class TestErrorCodeConsistency:
    """验证错误码在不同模块间的一致性"""

    def test_gateway_and_worker_use_same_error_codes(self):
        """验证 Gateway 和 Worker 使用相同的 ErrorCode 定义"""
        # 导入两个模块的 ErrorCode
        from engram.gateway.main import ErrorCode as GatewayErrorCode
        from engram.gateway.outbox_worker import ErrorCode as WorkerErrorCode
        
        # 验证是同一个类
        assert GatewayErrorCode is WorkerErrorCode, \
            "Gateway 和 Worker 应使用相同的 ErrorCode 类"

    def test_all_outbox_reasons_use_prefix(self):
        """验证所有 outbox 相关 reason 使用正确前缀"""
        outbox_codes = [
            ErrorCode.OUTBOX_FLUSH_SUCCESS,
            ErrorCode.OUTBOX_FLUSH_RETRY,
            ErrorCode.OUTBOX_FLUSH_DEAD,
            ErrorCode.OUTBOX_FLUSH_CONFLICT,
            ErrorCode.OUTBOX_FLUSH_DEDUP_HIT,
            ErrorCode.OUTBOX_FLUSH_DB_TIMEOUT,
            ErrorCode.OUTBOX_FLUSH_DB_ERROR,
        ]
        
        for code in outbox_codes:
            assert code.startswith("outbox_flush_"), \
                f"Outbox 错误码应以 'outbox_flush_' 开头: {code}"

    def test_all_openmemory_reasons_use_prefix(self):
        """验证所有 openmemory 相关 reason 使用正确前缀"""
        om_codes = [
            ErrorCode.OPENMEMORY_WRITE_FAILED_CONNECTION,
            ErrorCode.OPENMEMORY_WRITE_FAILED_API,
            ErrorCode.OPENMEMORY_WRITE_FAILED_GENERIC,
            ErrorCode.OPENMEMORY_WRITE_FAILED_UNKNOWN,
        ]
        
        for code in om_codes:
            assert code.startswith("openmemory_write_failed:"), \
                f"OpenMemory 错误码应以 'openmemory_write_failed:' 开头: {code}"


# ======================== OpenMemoryAPIError 含状态码测试 ========================

class TestOpenMemoryAPIErrorWithStatus:
    """验证 OpenMemoryAPIError 含状态码的错误码生成"""

    @pytest.fixture
    def mock_config(self):
        """模拟配置"""
        config = MagicMock()
        config.default_team_space = "team:test"
        config.project_key = "test_project"
        config.private_space_prefix = "private:"
        config.governance_admin_key = "test_admin_key"
        return config

    @pytest.mark.asyncio
    async def test_openmemory_api_error_with_status_404(self, mock_config):
        """关键路径测试: OpenMemory API 404 错误 -> reason 必须包含 api_error_404"""
        from engram.gateway.main import memory_store_impl
        from engram.gateway.openmemory_client import OpenMemoryAPIError
        
        captured_audits = []
        
        def capture_audit(**kwargs):
            captured_audits.append(kwargs)
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.get_client") as mock_get_client, \
             patch("engram.gateway.main.logbook_adapter") as mock_logbook_adapter, \
             patch("engram.gateway.main.create_engine_from_settings") as mock_create_engine:
            
            mock_get_config.return_value = mock_config
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {"team_write_enabled": True}
            mock_db.insert_audit = capture_audit
            mock_db.enqueue_outbox.return_value = 123
            mock_get_db.return_value = mock_db
            
            mock_decision = MagicMock()
            mock_decision.action.value = "allow"
            mock_decision.reason = "team_write_enabled"
            mock_decision.final_space = "team:test"
            mock_engine = MagicMock()
            mock_engine.decide.return_value = mock_decision
            mock_create_engine.return_value = mock_engine
            
            mock_logbook_adapter.check_dedup.return_value = None
            
            # 模拟 OpenMemory API 404 错误
            mock_client = MagicMock()
            mock_client.store.side_effect = OpenMemoryAPIError(
                message="Not Found",
                status_code=404,
                response=None
            )
            mock_get_client.return_value = mock_client
            
            result = await memory_store_impl(
                payload_md="test content",
                target_space="team:test",
            )
            
            assert result.ok is False
            assert len(captured_audits) == 1
            audit = captured_audits[0]
            
            # 关键断言: reason 必须为 openmemory_write_failed:api_error_404
            expected_reason = ErrorCode.openmemory_api_error(404)
            assert audit["reason"] == expected_reason, \
                f"OpenMemory API 404 错误时 reason 必须为 {expected_reason}，实际: {audit['reason']}"

    @pytest.mark.asyncio
    async def test_openmemory_api_error_with_status_500(self, mock_config):
        """关键路径测试: OpenMemory API 500 错误 -> reason 必须包含 api_error_500"""
        from engram.gateway.main import memory_store_impl
        from engram.gateway.openmemory_client import OpenMemoryAPIError
        
        captured_audits = []
        
        def capture_audit(**kwargs):
            captured_audits.append(kwargs)
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.get_client") as mock_get_client, \
             patch("engram.gateway.main.logbook_adapter") as mock_logbook_adapter, \
             patch("engram.gateway.main.create_engine_from_settings") as mock_create_engine:
            
            mock_get_config.return_value = mock_config
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {"team_write_enabled": True}
            mock_db.insert_audit = capture_audit
            mock_db.enqueue_outbox.return_value = 123
            mock_get_db.return_value = mock_db
            
            mock_decision = MagicMock()
            mock_decision.action.value = "allow"
            mock_decision.reason = "team_write_enabled"
            mock_decision.final_space = "team:test"
            mock_engine = MagicMock()
            mock_engine.decide.return_value = mock_decision
            mock_create_engine.return_value = mock_engine
            
            mock_logbook_adapter.check_dedup.return_value = None
            
            # 模拟 OpenMemory API 500 错误
            mock_client = MagicMock()
            mock_client.store.side_effect = OpenMemoryAPIError(
                message="Internal Server Error",
                status_code=500,
                response=None
            )
            mock_get_client.return_value = mock_client
            
            result = await memory_store_impl(
                payload_md="test content",
                target_space="team:test",
            )
            
            assert result.ok is False
            assert len(captured_audits) == 1
            audit = captured_audits[0]
            
            # 关键断言: reason 必须为 openmemory_write_failed:api_error_500
            expected_reason = ErrorCode.openmemory_api_error(500)
            assert audit["reason"] == expected_reason, \
                f"OpenMemory API 500 错误时 reason 必须为 {expected_reason}，实际: {audit['reason']}"


# ======================== 未知异常测试 ========================

class TestUnknownExceptionErrorCodes:
    """验证未知异常的错误码处理"""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.default_team_space = "team:test"
        config.project_key = "test_project"
        config.private_space_prefix = "private:"
        return config

    @pytest.mark.asyncio
    async def test_openmemory_generic_error_reason(self, mock_config):
        """关键路径测试: OpenMemory 通用错误 -> reason 必须为 openmemory_write_failed:openmemory_error"""
        from engram.gateway.main import memory_store_impl
        from engram.gateway.openmemory_client import OpenMemoryError
        
        captured_audits = []
        
        def capture_audit(**kwargs):
            captured_audits.append(kwargs)
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.get_client") as mock_get_client, \
             patch("engram.gateway.main.logbook_adapter") as mock_logbook_adapter, \
             patch("engram.gateway.main.create_engine_from_settings") as mock_create_engine:
            
            mock_get_config.return_value = mock_config
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {"team_write_enabled": True}
            mock_db.insert_audit = capture_audit
            mock_db.enqueue_outbox.return_value = 123
            mock_get_db.return_value = mock_db
            
            mock_decision = MagicMock()
            mock_decision.action.value = "allow"
            mock_decision.reason = "team_write_enabled"
            mock_decision.final_space = "team:test"
            mock_engine = MagicMock()
            mock_engine.decide.return_value = mock_decision
            mock_create_engine.return_value = mock_engine
            
            mock_logbook_adapter.check_dedup.return_value = None
            
            # 模拟 OpenMemory 通用错误
            mock_client = MagicMock()
            mock_client.store.side_effect = OpenMemoryError(
                message="Some generic error",
                status_code=None,
                response=None
            )
            mock_get_client.return_value = mock_client
            
            result = await memory_store_impl(
                payload_md="test content",
                target_space="team:test",
            )
            
            assert result.ok is False
            assert len(captured_audits) == 1
            audit = captured_audits[0]
            
            # 关键断言
            assert audit["reason"] == ErrorCode.OPENMEMORY_WRITE_FAILED_GENERIC, \
                f"OpenMemory 通用错误时 reason 必须为 {ErrorCode.OPENMEMORY_WRITE_FAILED_GENERIC}，实际: {audit['reason']}"


# ======================== DB Timeout vs DB Error 测试 ========================

class TestDBTimeoutVsDBError:
    """验证 DB timeout 和 DB error 的区分"""

    def test_db_timeout_error_classification(self):
        """测试 DB timeout 错误分类"""
        from engram.gateway.outbox_worker import _classify_db_error
        import psycopg.errors
        
        # 模拟 QueryCanceled 异常（statement_timeout）
        timeout_exc = psycopg.errors.QueryCanceled()
        error_type, reason = _classify_db_error(timeout_exc)
        
        assert error_type == "db_timeout"
        assert reason == ErrorCode.OUTBOX_FLUSH_DB_TIMEOUT, \
            f"DB timeout 时 reason 必须为 {ErrorCode.OUTBOX_FLUSH_DB_TIMEOUT}，实际: {reason}"

    def test_db_general_error_classification(self):
        """测试普通 DB 错误分类"""
        from engram.gateway.outbox_worker import _classify_db_error
        import psycopg.errors
        
        # 模拟普通数据库错误
        db_exc = psycopg.errors.OperationalError()
        error_type, reason = _classify_db_error(db_exc)
        
        assert error_type == "db_error"
        assert reason == ErrorCode.OUTBOX_FLUSH_DB_ERROR, \
            f"普通 DB 错误时 reason 必须为 {ErrorCode.OUTBOX_FLUSH_DB_ERROR}，实际: {reason}"

    def test_is_db_timeout_error_with_sqlstate(self):
        """测试通过 SQLSTATE 判断 DB timeout"""
        from engram.gateway.outbox_worker import _is_db_timeout_error
        
        # 创建模拟对象，设置 sqlstate
        class MockException(Exception):
            sqlstate = '57014'  # SQLSTATE for query_canceled
        
        exc = MockException()
        assert _is_db_timeout_error(exc) is True

    def test_is_db_timeout_error_with_pgcode(self):
        """测试通过 pgcode 判断 DB timeout（psycopg2 兼容）"""
        from engram.gateway.outbox_worker import _is_db_timeout_error
        
        class MockException(Exception):
            pgcode = '57014'
        
        exc = MockException()
        assert _is_db_timeout_error(exc) is True


# ======================== Conflict 路径 Reason 测试 ========================

class TestConflictPathErrorCodes:
    """验证 conflict 路径的错误码使用"""

    @pytest.fixture
    def config(self):
        from engram.gateway.outbox_worker import WorkerConfig
        return WorkerConfig(max_retries=3, jitter_factor=0.0)

    def make_outbox_item(
        self,
        outbox_id: int = 1,
        target_space: str = "private:user_001",
        payload_md: str = "# Test Memory",
        payload_sha: str = "a" * 64,
        retry_count: int = 0,
    ):
        from engram.gateway.logbook_adapter import OutboxItem
        return OutboxItem(
            outbox_id=outbox_id,
            item_id=None,
            target_space=target_space,
            payload_md=payload_md,
            payload_sha=payload_sha,
            retry_count=retry_count,
        )

    def test_outbox_flush_conflict_reason(self, config):
        """关键路径测试: Outbox lease 冲突 -> reason 必须为 outbox_flush_conflict"""
        from engram.gateway.outbox_worker import _handle_conflict
        
        captured_audits = []
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            # 模拟 get_outbox_by_id 返回被其他 worker 占用的状态
            mock_adapter.get_outbox_by_id.return_value = {
                "status": "pending",
                "locked_by": "other-worker",
                "last_error": None,
            }
            mock_adapter.insert_write_audit = lambda **kwargs: captured_audits.append(kwargs) or 1
            
            result = _handle_conflict(
                outbox_id=1,
                worker_id="my-worker",
                attempt_id="attempt-123",
                user_id="user_001",
                target_space="private:user_001",
                payload_sha="a" * 64,
                intended_action="success",
                correlation_id="corr-456",
            )
            
            # 验证返回
            assert result.conflict is True
            assert result.action == "redirect"
            
            # 关键断言: reason 必须为 outbox_flush_conflict
            assert result.reason == ErrorCode.OUTBOX_FLUSH_CONFLICT, \
                f"Lease 冲突时 reason 必须为 {ErrorCode.OUTBOX_FLUSH_CONFLICT}，实际: {result.reason}"
            
            # 验证审计记录
            assert len(captured_audits) == 1
            assert captured_audits[0]["reason"] == ErrorCode.OUTBOX_FLUSH_CONFLICT

    def test_conflict_on_dedup_hit_ack(self, config):
        """测试 dedup_hit 时 ack 失败的 conflict"""
        from engram.gateway.outbox_worker import process_single_item
        
        item = self.make_outbox_item(outbox_id=10)
        mock_client = MagicMock()
        
        captured_audits = []
        
        with patch("engram.gateway.outbox_worker.logbook_adapter") as mock_adapter:
            # 模拟 dedup 命中
            mock_adapter.check_dedup.return_value = {
                "outbox_id": 100,
                "status": "sent",
                "last_error": "memory_id=mem_original",
            }
            # 模拟 ack_sent 返回 False（冲突）
            mock_adapter.ack_sent.return_value = False
            mock_adapter.get_outbox_by_id.return_value = {
                "status": "sent",
                "locked_by": "other-worker",
            }
            mock_adapter.insert_write_audit = lambda **kwargs: captured_audits.append(kwargs) or 1
            
            result = process_single_item(item, "my-worker", mock_client, config)
            
            assert result.conflict is True
            assert result.reason == ErrorCode.OUTBOX_FLUSH_CONFLICT


# ======================== Governance 更新拒绝/缺凭证测试 ========================

class TestGovernanceUpdateErrorCodes:
    """验证 governance_update 拒绝和缺凭证的错误码"""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.project_key = "test_project"
        config.governance_admin_key = "real_admin_key"
        return config

    @pytest.mark.asyncio
    async def test_governance_missing_credentials_reason(self, mock_config):
        """关键路径测试: 治理更新缺少凭证 -> reason 必须为 governance_update:missing_credentials"""
        from engram.gateway.main import governance_update_impl
        
        captured_audits = []
        
        def capture_audit(**kwargs):
            captured_audits.append(kwargs)
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db:
            
            mock_get_config.return_value = mock_config
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {"allowlist_users": []},
            }
            mock_db.insert_audit = capture_audit
            mock_get_db.return_value = mock_db
            
            # 不提供任何凭证
            result = await governance_update_impl(
                team_write_enabled=False,
                admin_key=None,
                actor_user_id=None,
            )
            
            assert result.ok is False
            assert result.action == "reject"
            
            # 关键断言
            assert len(captured_audits) == 1
            assert captured_audits[0]["reason"] == ErrorCode.GOVERNANCE_UPDATE_MISSING_CREDENTIALS, \
                f"缺少凭证时 reason 必须为 {ErrorCode.GOVERNANCE_UPDATE_MISSING_CREDENTIALS}，实际: {captured_audits[0]['reason']}"

    @pytest.mark.asyncio
    async def test_governance_invalid_admin_key_reason(self, mock_config):
        """关键路径测试: 无效的 admin_key -> reason 必须为 governance_update:invalid_admin_key"""
        from engram.gateway.main import governance_update_impl
        
        captured_audits = []
        
        def capture_audit(**kwargs):
            captured_audits.append(kwargs)
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db:
            
            mock_get_config.return_value = mock_config
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {"allowlist_users": []},
            }
            mock_db.insert_audit = capture_audit
            mock_get_db.return_value = mock_db
            
            # 提供无效的 admin_key
            result = await governance_update_impl(
                team_write_enabled=False,
                admin_key="wrong_key",
                actor_user_id=None,
            )
            
            assert result.ok is False
            assert result.action == "reject"
            
            assert len(captured_audits) == 1
            assert captured_audits[0]["reason"] == ErrorCode.GOVERNANCE_UPDATE_INVALID_ADMIN_KEY, \
                f"无效 admin_key 时 reason 必须为 {ErrorCode.GOVERNANCE_UPDATE_INVALID_ADMIN_KEY}，实际: {captured_audits[0]['reason']}"

    @pytest.mark.asyncio
    async def test_governance_user_not_in_allowlist_reason(self, mock_config):
        """关键路径测试: 用户不在 allowlist -> reason 必须为 governance_update:user_not_in_allowlist"""
        from engram.gateway.main import governance_update_impl
        
        captured_audits = []
        
        def capture_audit(**kwargs):
            captured_audits.append(kwargs)
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db:
            
            mock_get_config.return_value = mock_config
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {"allowlist_users": ["admin1", "admin2"]},
            }
            mock_db.insert_audit = capture_audit
            mock_get_db.return_value = mock_db
            
            # 提供不在 allowlist 中的 user_id
            result = await governance_update_impl(
                team_write_enabled=False,
                admin_key=None,
                actor_user_id="not_in_list_user",
            )
            
            assert result.ok is False
            assert result.action == "reject"
            
            assert len(captured_audits) == 1
            assert captured_audits[0]["reason"] == ErrorCode.GOVERNANCE_UPDATE_USER_NOT_IN_ALLOWLIST, \
                f"用户不在 allowlist 时 reason 必须为 {ErrorCode.GOVERNANCE_UPDATE_USER_NOT_IN_ALLOWLIST}，实际: {captured_audits[0]['reason']}"

    @pytest.mark.asyncio
    async def test_governance_internal_error_reason(self, mock_config):
        """关键路径测试: 治理更新内部错误 -> reason 必须为 governance_update:internal_error"""
        from engram.gateway.main import governance_update_impl
        
        captured_audits = []
        
        def capture_audit(**kwargs):
            captured_audits.append(kwargs)
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.logbook_adapter") as mock_logbook_adapter:
            
            mock_get_config.return_value = mock_config
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {"allowlist_users": []},
            }
            mock_db.insert_audit = capture_audit
            mock_db.get_settings.return_value = {}
            mock_get_db.return_value = mock_db
            
            # 模拟内部错误
            mock_adapter = MagicMock()
            mock_adapter.upsert_settings.side_effect = Exception("DB connection lost")
            mock_logbook_adapter.get_adapter.return_value = mock_adapter
            
            # 提供有效的 admin_key
            result = await governance_update_impl(
                team_write_enabled=False,
                admin_key="real_admin_key",
                actor_user_id=None,
            )
            
            assert result.ok is False
            assert result.action == "reject"
            
            # 应该有两条审计：尝试前不会写入，仅在失败后写入
            assert len(captured_audits) == 1
            assert captured_audits[0]["reason"] == ErrorCode.GOVERNANCE_UPDATE_INTERNAL_ERROR, \
                f"内部错误时 reason 必须为 {ErrorCode.GOVERNANCE_UPDATE_INTERNAL_ERROR}，实际: {captured_audits[0]['reason']}"


# ======================== Reconcile Outbox ErrorCode 测试 ========================

class TestReconcileOutboxErrorCodes:
    """验证 reconcile_outbox 模块的 ErrorCode 使用"""

    def test_outbox_stale_error_code_exists(self):
        """验证 OUTBOX_STALE 错误码存在"""
        assert hasattr(ErrorCode, 'OUTBOX_STALE')
        assert ErrorCode.OUTBOX_STALE == "outbox_stale"

    def test_reconcile_uses_error_code_constants(self):
        """验证 reconcile_outbox 模块导入了 ErrorCode"""
        from gateway import reconcile_outbox
        # 验证模块已导入 ErrorCode
        assert hasattr(reconcile_outbox, 'ErrorCode')


# ======================== 统一响应契约测试 ========================

class TestUnifiedResponseContract:
    """
    验证统一响应契约（详见 docs/gateway/07_capability_boundary.md）
    
    契约要求：
    - correlation_id 必须在所有响应中返回
    - action=deferred 时 outbox_id 必须返回
    - 字段类型稳定（outbox_id 始终为 int 或 None）
    """

    @pytest.fixture
    def mock_config(self):
        """模拟配置"""
        config = MagicMock()
        config.default_team_space = "team:test"
        config.project_key = "test_project"
        config.private_space_prefix = "private:"
        config.governance_admin_key = "test_admin_key"
        return config

    @pytest.mark.asyncio
    async def test_deferred_response_contract(self, mock_config):
        """契约测试: action=deferred 时必须返回 outbox_id 和 correlation_id"""
        from engram.gateway.main import memory_store_impl
        from engram.gateway.openmemory_client import OpenMemoryConnectionError
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.get_client") as mock_get_client, \
             patch("engram.gateway.main.logbook_adapter") as mock_logbook_adapter, \
             patch("engram.gateway.main.create_engine_from_settings") as mock_create_engine:
            
            mock_get_config.return_value = mock_config
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {"team_write_enabled": True}
            mock_db.insert_audit = MagicMock()
            mock_db.enqueue_outbox.return_value = 456  # 模拟 outbox_id
            mock_get_db.return_value = mock_db
            
            mock_decision = MagicMock()
            mock_decision.action.value = "allow"
            mock_decision.reason = "team_write_enabled"
            mock_decision.final_space = "team:test"
            mock_engine = MagicMock()
            mock_engine.decide.return_value = mock_decision
            mock_create_engine.return_value = mock_engine
            
            mock_logbook_adapter.check_dedup.return_value = None
            
            mock_client = MagicMock()
            mock_client.store.side_effect = OpenMemoryConnectionError("Connection refused")
            mock_get_client.return_value = mock_client
            
            result = await memory_store_impl(
                payload_md="test content",
                target_space="team:test",
            )
            
            # 契约断言：deferred 响应结构
            assert result.action == "deferred", \
                f"OpenMemory 失败应返回 action='deferred'，实际: {result.action}"
            assert result.outbox_id == 456, \
                f"deferred 响应必须包含正确的 outbox_id，实际: {result.outbox_id}"
            assert isinstance(result.outbox_id, int), \
                f"outbox_id 类型必须为 int，实际: {type(result.outbox_id)}"
            assert result.correlation_id is not None, \
                f"deferred 响应必须包含 correlation_id"
            assert result.correlation_id.startswith("corr-"), \
                f"correlation_id 格式不正确: {result.correlation_id}"

    @pytest.mark.asyncio
    async def test_allow_response_contract(self, mock_config):
        """契约测试: action=allow 时必须返回 memory_id 和 correlation_id"""
        from engram.gateway.main import memory_store_impl
        from engram.gateway.openmemory_client import StoreResult
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.get_client") as mock_get_client, \
             patch("engram.gateway.main.logbook_adapter") as mock_logbook_adapter, \
             patch("engram.gateway.main.create_engine_from_settings") as mock_create_engine:
            
            mock_get_config.return_value = mock_config
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {"team_write_enabled": True}
            mock_db.insert_audit = MagicMock()
            mock_get_db.return_value = mock_db
            
            mock_decision = MagicMock()
            mock_decision.action.value = "allow"
            mock_decision.reason = "team_write_enabled"
            mock_decision.final_space = "team:test"
            mock_engine = MagicMock()
            mock_engine.decide.return_value = mock_decision
            mock_create_engine.return_value = mock_engine
            
            mock_logbook_adapter.check_dedup.return_value = None
            
            mock_client = MagicMock()
            mock_client.store.return_value = StoreResult(
                success=True,
                memory_id="mem_test_123",
                error=None
            )
            mock_get_client.return_value = mock_client
            
            result = await memory_store_impl(
                payload_md="test content",
                target_space="team:test",
            )
            
            # 契约断言：allow 响应结构
            assert result.ok is True, f"成功写入应返回 ok=True，实际: {result.ok}"
            assert result.action == "allow", f"成功写入应返回 action='allow'，实际: {result.action}"
            assert result.memory_id == "mem_test_123", \
                f"allow 响应必须包含 memory_id，实际: {result.memory_id}"
            assert result.outbox_id is None, \
                f"allow 响应 outbox_id 应为 None，实际: {result.outbox_id}"
            assert result.correlation_id is not None, \
                f"allow 响应必须包含 correlation_id"
            assert result.space_written == "team:test", \
                f"allow 响应必须包含 space_written，实际: {result.space_written}"

    @pytest.mark.asyncio
    async def test_reject_response_contract(self, mock_config):
        """契约测试: action=reject 时必须返回 correlation_id"""
        from engram.gateway.main import memory_store_impl
        from engram.gateway.policy import PolicyAction
        
        with patch("engram.gateway.main.get_config") as mock_get_config, \
             patch("engram.gateway.main.get_db") as mock_get_db, \
             patch("engram.gateway.main.logbook_adapter") as mock_logbook_adapter, \
             patch("engram.gateway.main.create_engine_from_settings") as mock_create_engine:
            
            mock_get_config.return_value = mock_config
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {"team_write_enabled": False}
            mock_db.insert_audit = MagicMock()
            mock_get_db.return_value = mock_db
            
            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.REJECT
            mock_decision.reason = "team_write_disabled"
            mock_decision.final_space = None
            mock_engine = MagicMock()
            mock_engine.decide.return_value = mock_decision
            mock_create_engine.return_value = mock_engine
            
            mock_logbook_adapter.check_dedup.return_value = None
            
            result = await memory_store_impl(
                payload_md="test content",
                target_space="team:test",
            )
            
            # 契约断言：reject 响应结构
            assert result.ok is False, f"拒绝应返回 ok=False，实际: {result.ok}"
            assert result.action == "reject", f"拒绝应返回 action='reject'，实际: {result.action}"
            assert result.memory_id is None, \
                f"reject 响应 memory_id 应为 None，实际: {result.memory_id}"
            assert result.outbox_id is None, \
                f"reject 响应 outbox_id 应为 None，实际: {result.outbox_id}"
            assert result.correlation_id is not None, \
                f"reject 响应必须包含 correlation_id"

    def test_response_model_fields_exist(self):
        """验证 MemoryStoreResponse 模型包含所有必需字段"""
        from engram.gateway.main import MemoryStoreResponse
        
        # 获取模型字段
        fields = MemoryStoreResponse.model_fields
        
        # 验证必需字段存在
        required_fields = ["ok", "action", "space_written", "memory_id", 
                          "outbox_id", "correlation_id", "evidence_refs", "message"]
        for field in required_fields:
            assert field in fields, f"MemoryStoreResponse 缺少字段: {field}"
        
        # 验证字段类型注解
        assert fields["outbox_id"].annotation == Optional[int], \
            f"outbox_id 类型应为 Optional[int]，实际: {fields['outbox_id'].annotation}"
        assert fields["correlation_id"].annotation == Optional[str], \
            f"correlation_id 类型应为 Optional[str]，实际: {fields['correlation_id'].annotation}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
