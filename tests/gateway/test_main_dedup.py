# -*- coding: utf-8 -*-
"""
memory_store_impl Dedupe 测试

测试覆盖:
- 写入前 dedupe check
- dedup_hit 时直接返回并写入审计
- 无重复时继续正常流程
- strict evidence 校验
- 审计不可丢（audit must not be lost）
- OpenMemory 不可用时 deferred/outbox
"""

import secrets
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from engram.gateway.di import GatewayDeps
from engram.gateway.handlers.memory_store import memory_store_impl
from engram.gateway.services.hash_utils import compute_payload_sha

# 导入 Fake 依赖
from tests.gateway.fakes import (
    FakeGatewayConfig,
    FakeLogbookDatabase,
    FakeOpenMemoryClient,
)

# Mock 路径：handlers 模块使用的依赖
HANDLER_MODULE = "engram.gateway.handlers.memory_store"


def _test_correlation_id():
    """生成测试用的 correlation_id"""
    return f"corr-{secrets.token_hex(8)}"


# ==================== 测试用的 Mock 配置 ====================
#
# 注意: 推荐使用 conftest.py 中的 gateway_deps fixture 进行依赖注入。
# 如果需要自定义配置，可以使用 FakeGatewayConfig 或自定义 dataclass。
#
# 使用示例:
#   @pytest.mark.asyncio
#   async def test_with_gateway_deps(gateway_deps, test_correlation_id):
#       result = await memory_store_impl(
#           payload_md="test",
#           correlation_id=test_correlation_id,
#           deps=gateway_deps,
#       )
#
# 自定义配置示例:
#   from tests.gateway.fakes import FakeGatewayConfig
#   custom_config = FakeGatewayConfig(default_team_space="team:custom")
#   deps = GatewayDeps.for_testing(config=custom_config, db=mock_db)


@dataclass
class MockGatewayConfig:
    """
    测试用 Mock Gateway 配置

    注意: 对于新测试，推荐使用 tests/gateway/fakes.py 中的 FakeGatewayConfig，
    或直接使用 conftest.py 中的 fake_gateway_config fixture。
    """

    project_key: str = "test_project"
    postgres_dsn: str = "postgresql://fake:fake@localhost/fakedb"
    default_team_space: str = "team:default"
    private_space_prefix: str = "private:"
    openmemory_base_url: str = "http://fake-openmemory:8080"
    openmemory_api_key: Optional[str] = "fake_api_key"
    governance_admin_key: Optional[str] = None
    unknown_actor_policy: str = "degrade"
    # 新增必需字段
    gateway_port: int = 8787
    auto_migrate_on_startup: bool = False
    logbook_check_on_startup: bool = False
    minio_audit_webhook_auth_token: Optional[str] = None
    minio_audit_max_payload_size: int = 1024 * 1024
    validate_evidence_refs: bool = False
    strict_mode_enforce_validate_refs: bool = True


class TestMemoryStoreDedup:
    """memory_store_impl dedupe 测试"""

    @pytest.mark.asyncio
    async def test_dedup_hit_returns_early(self):
        """存在已成功写入的记录时直接返回"""
        payload_md = "# Test content for dedup"
        target_space = "team:test"
        payload_sha = compute_payload_sha(payload_md)

        # 创建 mock 配置和 db
        mock_config = MockGatewayConfig(default_team_space="team:default")

        mock_db = MagicMock()
        mock_db.insert_audit = MagicMock(return_value=1)

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = {
            "outbox_id": 100,
            "target_space": target_space,
            "payload_sha": payload_sha,
            "status": "sent",
            "last_error": "memory_id=mem_existing_123",
        }

        # 创建 deps（通过 for_testing 注入所有依赖）
        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
        )

        # 执行
        result = await memory_store_impl(
            payload_md=payload_md,
            target_space=target_space,
            correlation_id=_test_correlation_id(),
            deps=deps,
        )

        # 验证
        assert result.ok is True
        assert result.action == "allow"
        assert result.memory_id == "mem_existing_123"
        assert result.message == "dedup_hit: 已存在相同内容的成功写入记录"

        # 验证 check_dedup 被调用
        mock_adapter.check_dedup.assert_called_once_with(
            target_space=target_space,
            payload_sha=payload_sha,
        )

        # 验证审计日志写入
        mock_db.insert_audit.assert_called_once()
        audit_call = mock_db.insert_audit.call_args[1]
        assert audit_call["action"] == "allow"
        assert audit_call["reason"] == "dedup_hit"
        assert audit_call["payload_sha"] == payload_sha

        # 验证 evidence_refs_json 包含 extra.correlation_id
        evidence = audit_call["evidence_refs_json"]
        assert "extra" in evidence, "evidence_refs_json 应包含 extra 字段"
        assert "correlation_id" in evidence["extra"], "extra 应包含 correlation_id"
        assert evidence["extra"]["correlation_id"].startswith("corr-"), (
            "correlation_id 应以 corr- 开头"
        )
        assert evidence.get("source") == "gateway", "source 应为 gateway"

    @pytest.mark.asyncio
    async def test_dedup_hit_audit_contains_original_outbox_id(self):
        """dedup_hit 审计日志包含原始 outbox_id"""
        payload_md = "# Another test"
        target_space = "private:alice"
        payload_sha = compute_payload_sha(payload_md)

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.insert_audit = MagicMock(return_value=1)

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = {
            "outbox_id": 200,
            "target_space": target_space,
            "payload_sha": payload_sha,
            "status": "sent",
            "last_error": None,
        }

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
        )

        await memory_store_impl(
            payload_md=payload_md,
            target_space=target_space,
            evidence_refs=["ref1", "ref2"],
            correlation_id=_test_correlation_id(),
            deps=deps,
        )

        # 验证审计日志包含 original_outbox_id 和 evidence_refs
        audit_call = mock_db.insert_audit.call_args[1]
        evidence = audit_call["evidence_refs_json"]
        assert evidence["original_outbox_id"] == 200
        assert evidence["refs"] == ["ref1", "ref2"]
        assert evidence.get("source") == "gateway"

        # 验证 extra 包含 correlation_id
        assert "extra" in evidence, "evidence_refs_json 应包含 extra 字段"
        assert "correlation_id" in evidence["extra"], "extra 应包含 correlation_id"

    @pytest.mark.asyncio
    async def test_no_dedup_continues_to_openmemory(self):
        """无重复记录时继续调用 OpenMemory"""
        payload_md = "# Unique content"
        target_space = "team:unique"

        mock_config = MockGatewayConfig()

        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {},
        }
        mock_db.insert_audit = MagicMock(return_value=1)

        # 模拟 OpenMemory 客户端
        mock_client = MagicMock()
        mock_store_result = MagicMock()
        mock_store_result.success = True
        mock_store_result.memory_id = "mem_new_456"
        mock_client.store.return_value = mock_store_result

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            openmemory_client=mock_client,
            logbook_adapter=mock_adapter,
        )

        with patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine:
            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "test"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 验证 check_dedup 被调用
            mock_adapter.check_dedup.assert_called_once()

            # 验证 OpenMemory 被调用
            mock_client.store.assert_called_once()

            # 验证结果
            assert result.ok is True
            assert result.memory_id == "mem_new_456"

    @pytest.mark.asyncio
    async def test_dedup_with_default_target_space(self):
        """使用默认 target_space 时 dedupe 正常工作"""
        payload_md = "# Default space test"

        mock_config = MockGatewayConfig(default_team_space="team:default_project")
        mock_db = MagicMock()
        mock_db.insert_audit = MagicMock(return_value=1)

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = {
            "outbox_id": 300,
            "target_space": "team:default_project",
            "payload_sha": compute_payload_sha(payload_md),
            "status": "sent",
            "last_error": None,
        }

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
        )

        # 不传入 target_space，使用默认值
        result = await memory_store_impl(
            payload_md=payload_md,
            target_space=None,
            correlation_id=_test_correlation_id(),
            deps=deps,
        )

        # 验证使用默认 target_space 进行 check_dedup
        mock_adapter.check_dedup.assert_called_once()
        call_kwargs = mock_adapter.check_dedup.call_args[1]
        assert call_kwargs["target_space"] == "team:default_project"

        assert result.ok is True
        assert result.action == "allow"


# ==================== strict evidence 校验测试 ====================


class TestStrictEvidenceValidation:
    """
    strict evidence 校验测试

    验证 evidence_mode="strict" 时：
    1. 缺少 sha256 字段应触发 reject
    2. sha256 格式无效应触发 reject
    3. 合法 evidence 应通过
    """

    @pytest.mark.asyncio
    async def test_strict_mode_rejects_missing_sha256(self):
        """
        strict 模式下缺少 sha256 字段应触发 reject

        契约: evidence 必须包含 sha256 字段
        """
        payload_md = "# Test strict mode"
        target_space = "team:test"

        # 创建缺少 sha256 的 v2 evidence
        evidence_without_sha = [
            {
                "type": "external",
                "uri": "commit:abc123",
                # 缺少 sha256 字段
            }
        ]

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {"evidence_mode": "strict"},  # strict 模式
        }
        mock_db.insert_audit = MagicMock(return_value=1)

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
        )

        with patch(f"{HANDLER_MODULE}.write_audit_or_raise") as mock_write_audit:
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                evidence=evidence_without_sha,
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 验证被拒绝
            assert result.ok is False
            assert result.action == "reject"
            assert "strict" in result.message.lower() or "evidence" in result.message.lower()

            # 验证审计日志被写入
            mock_write_audit.assert_called_once()
            audit_call = mock_write_audit.call_args[1]
            assert audit_call["action"] == "reject"
            assert "EVIDENCE" in audit_call["reason"].upper()

    @pytest.mark.asyncio
    async def test_strict_mode_rejects_invalid_sha256_format(self):
        """
        strict 模式下 sha256 格式无效应触发 reject

        契约: sha256 必须是 64 字符的十六进制字符串
        """
        payload_md = "# Test invalid sha256"
        target_space = "team:test"

        # 创建 sha256 格式无效的 evidence
        evidence_invalid_sha = [
            {
                "type": "external",
                "uri": "commit:abc123",
                "sha256": "not_a_valid_sha256",  # 无效格式
            }
        ]

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {"evidence_mode": "strict"},
        }
        mock_db.insert_audit = MagicMock(return_value=1)

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
        )

        with patch(f"{HANDLER_MODULE}.write_audit_or_raise") as mock_write_audit:
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                evidence=evidence_invalid_sha,
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 验证被拒绝
            assert result.ok is False
            assert result.action == "reject"

            # 验证审计日志被写入
            mock_write_audit.assert_called_once()

    @pytest.mark.asyncio
    async def test_strict_mode_allows_valid_evidence(self):
        """
        strict 模式下合法 evidence 应通过
        """
        payload_md = "# Test valid evidence"
        target_space = "team:test"

        # 创建合法的 v2 evidence
        valid_sha256 = "a" * 64  # 64 字符十六进制
        valid_evidence = [
            {
                "type": "external",
                "uri": "commit:abc123",
                "sha256": valid_sha256,
            }
        ]

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {"evidence_mode": "strict"},
        }
        mock_db.insert_audit = MagicMock(return_value=1)

        # 模拟 OpenMemory 成功
        mock_client = MagicMock()
        mock_store_result = MagicMock()
        mock_store_result.success = True
        mock_store_result.memory_id = "mem_strict_valid"
        mock_client.store.return_value = mock_store_result

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            openmemory_client=mock_client,
            logbook_adapter=mock_adapter,
        )

        with patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine:
            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                evidence=valid_evidence,
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 验证通过
            assert result.ok is True
            assert result.action == "allow"
            assert result.memory_id == "mem_strict_valid"

    @pytest.mark.asyncio
    async def test_compat_mode_allows_missing_sha256(self):
        """
        compat 模式下缺少 sha256 应通过（向后兼容）
        """
        payload_md = "# Test compat mode"
        target_space = "team:test"

        # 缺少 sha256 的 evidence
        evidence_without_sha = [
            {
                "type": "external",
                "uri": "commit:abc123",
            }
        ]

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {"evidence_mode": "compat"},  # compat 模式
        }
        mock_db.insert_audit = MagicMock(return_value=1)

        # 模拟 OpenMemory 成功
        mock_client = MagicMock()
        mock_store_result = MagicMock()
        mock_store_result.success = True
        mock_store_result.memory_id = "mem_compat"
        mock_client.store.return_value = mock_store_result

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            openmemory_client=mock_client,
            logbook_adapter=mock_adapter,
        )

        with patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine:
            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                evidence=evidence_without_sha,
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # compat 模式应通过
            assert result.ok is True
            assert result.action == "allow"


# ==================== 审计不可丢测试 ====================


class TestAuditMustNotBeLost:
    """
    审计不可丢测试

    验证关键场景下审计记录不会丢失：
    1. dedup_hit 必须写入审计
    2. policy reject 必须写入审计
    3. OpenMemory 失败时必须写入审计
    4. 审计写入失败时应阻断操作
    """

    @pytest.mark.asyncio
    async def test_dedup_hit_audit_not_lost(self):
        """
        dedup_hit 时审计不可丢
        """
        payload_md = "# Audit not lost - dedup"
        target_space = "team:test"

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.insert_audit = MagicMock(return_value=1)

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = {
            "outbox_id": 100,
            "target_space": target_space,
            "payload_sha": compute_payload_sha(payload_md),
            "status": "sent",
            "last_error": "memory_id=mem_123",
        }

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
        )

        await memory_store_impl(
            payload_md=payload_md,
            target_space=target_space,
            correlation_id=_test_correlation_id(),
            deps=deps,
        )

        # 关键断言：审计必须被调用
        assert mock_db.insert_audit.called, "dedup_hit 时审计必须被调用"

        # 验证审计内容
        audit_call = mock_db.insert_audit.call_args[1]
        assert audit_call["action"] == "allow"
        assert audit_call["reason"] == "dedup_hit"

    @pytest.mark.asyncio
    async def test_policy_reject_audit_not_lost(self):
        """
        policy reject 时审计不可丢
        """
        payload_md = "# Audit not lost - policy reject"

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {},
        }

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            logbook_adapter=mock_adapter,
        )

        with (
            patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
            patch(f"{HANDLER_MODULE}.write_audit_or_raise") as mock_write_audit,
        ):
            # 模拟策略拒绝
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.REJECT
            mock_decision.reason = "unknown_space_type"
            mock_decision.final_space = None
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space="unknown:space",
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 关键断言：审计必须被调用
            assert mock_write_audit.called, "policy reject 时审计必须被调用"

            # 验证返回
            assert result.ok is False
            assert result.action == "reject"

    @pytest.mark.asyncio
    async def test_openmemory_failure_audit_not_lost(self):
        """
        OpenMemory 失败时审计不可丢
        """
        payload_md = "# Audit not lost - OM failure"
        target_space = "team:test"

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {},
        }
        mock_db.insert_audit = MagicMock(return_value=1)
        mock_db.enqueue_outbox = MagicMock(return_value=100)

        # 模拟 OpenMemory 连接失败
        from engram.gateway.openmemory_client import OpenMemoryConnectionError

        mock_client = MagicMock()
        mock_client.store.side_effect = OpenMemoryConnectionError("连接超时")

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            openmemory_client=mock_client,
            logbook_adapter=mock_adapter,
        )

        with patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine:
            # 模拟策略通过
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 关键断言：审计必须被调用
            assert mock_db.insert_audit.called, "OpenMemory 失败时审计必须被调用"

            # 验证返回 deferred
            assert result.action == "deferred"
            assert result.outbox_id == 100

    @pytest.mark.asyncio
    async def test_audit_failure_blocks_operation(self):
        """
        审计写入失败时应阻断操作

        契约: 审计写入失败 → 操作阻断，返回 error
        """
        payload_md = "# Audit failure blocks"
        target_space = "team:test"

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {},
        }

        # 模拟 OpenMemory 成功
        mock_client = MagicMock()
        mock_store_result = MagicMock()
        mock_store_result.success = True
        mock_store_result.memory_id = "mem_should_not_return"
        mock_client.store.return_value = mock_store_result

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            openmemory_client=mock_client,
            logbook_adapter=mock_adapter,
        )

        with (
            patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine,
            patch(f"{HANDLER_MODULE}.write_audit_or_raise") as mock_write_audit,
        ):
            # 模拟策略通过
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟审计写入失败
            from engram.gateway.audit_event import AuditWriteError

            mock_write_audit.side_effect = AuditWriteError("数据库连接失败", "AUDIT_DB_ERROR")

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 关键断言：操作应被阻断
            assert result.ok is False
            assert result.action == "error"
            assert "审计写入失败" in result.message


# ==================== OpenMemory 不可用时 deferred/outbox 测试 ====================


class TestOpenMemoryUnavailableDeferred:
    """
    OpenMemory 不可用时 deferred/outbox 测试

    验证：
    1. 连接失败 → deferred + outbox_id
    2. API 5xx 错误 → deferred + outbox_id
    3. API 4xx 错误 → deferred + outbox_id
    4. 响应包含 correlation_id
    """

    @pytest.mark.asyncio
    async def test_connection_error_triggers_deferred(self):
        """
        连接失败触发 deferred 响应
        """
        payload_md = "# Connection error test"
        target_space = "team:test"

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {},
        }
        mock_db.insert_audit = MagicMock(return_value=1)
        mock_db.enqueue_outbox = MagicMock(return_value=200)

        # 模拟连接失败
        from engram.gateway.openmemory_client import OpenMemoryConnectionError

        mock_client = MagicMock()
        mock_client.store.side_effect = OpenMemoryConnectionError("连接超时")

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            openmemory_client=mock_client,
            logbook_adapter=mock_adapter,
        )

        with patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine:
            # 模拟策略通过
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            test_corr_id = _test_correlation_id()
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                correlation_id=test_corr_id,
                deps=deps,
            )

            # 验证 deferred 响应
            assert result.ok is False
            assert result.action == "deferred"
            assert result.outbox_id == 200
            assert result.correlation_id == test_corr_id
            assert result.correlation_id.startswith("corr-")

            # 验证 outbox 入队
            mock_db.enqueue_outbox.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_5xx_error_triggers_deferred(self):
        """
        API 5xx 错误触发 deferred 响应
        """
        payload_md = "# API 5xx error test"
        target_space = "team:test"

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {},
        }
        mock_db.insert_audit = MagicMock(return_value=1)
        mock_db.enqueue_outbox = MagicMock(return_value=300)

        # 模拟 API 503 错误
        from engram.gateway.openmemory_client import OpenMemoryAPIError

        mock_client = MagicMock()
        mock_client.store.side_effect = OpenMemoryAPIError(
            message="Service Unavailable",
            status_code=503,
            response={"error": "Service Unavailable"},
        )

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            openmemory_client=mock_client,
            logbook_adapter=mock_adapter,
        )

        with patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine:
            # 模拟策略通过
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 验证 deferred 响应
            assert result.ok is False
            assert result.action == "deferred"
            assert result.outbox_id == 300
            assert result.correlation_id is not None

    @pytest.mark.asyncio
    async def test_deferred_response_contains_correct_outbox_id(self):
        """
        deferred 响应包含正确的 outbox_id
        """
        payload_md = "# Correct outbox_id test"
        target_space = "team:test"
        expected_outbox_id = 12345

        mock_config = MockGatewayConfig()
        mock_db = MagicMock()
        mock_db.get_or_create_settings.return_value = {
            "team_write_enabled": True,
            "policy_json": {},
        }
        mock_db.insert_audit = MagicMock(return_value=1)
        mock_db.enqueue_outbox = MagicMock(return_value=expected_outbox_id)

        # 模拟连接失败
        from engram.gateway.openmemory_client import OpenMemoryConnectionError

        mock_client = MagicMock()
        mock_client.store.side_effect = OpenMemoryConnectionError("连接超时")

        # 创建 mock logbook_adapter
        mock_adapter = MagicMock()
        mock_adapter.check_dedup.return_value = None

        deps = GatewayDeps.for_testing(
            config=mock_config,
            db=mock_db,
            openmemory_client=mock_client,
            logbook_adapter=mock_adapter,
        )

        with patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine:
            # 模拟策略通过
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 关键断言：outbox_id 必须正确
            assert result.outbox_id == expected_outbox_id
            assert isinstance(result.outbox_id, int)


# ==================== 使用 Fake 依赖的集成测试 ====================


class TestMemoryStoreWithFakeDependencies:
    """
    使用 Fake 依赖的集成测试

    验证 memory_store_impl 的依赖注入工作正常
    """

    @pytest.mark.asyncio
    async def test_with_fake_config_and_db(self):
        """
        使用 FakeGatewayConfig 和 FakeLogbookDatabase
        """
        payload_md = "# Test with fakes"

        # 创建 fake 依赖
        fake_config = FakeGatewayConfig(
            project_key="fake_project",
            default_team_space="team:fake_project",
        )

        fake_db = FakeLogbookDatabase()
        fake_db.configure_settings(
            team_write_enabled=True,
            policy_json={},
        )

        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="fake_mem_id")

        # 创建 mock logbook_adapter（使用 fakes 中的 FakeLogbookAdapter）
        from tests.gateway.fakes import FakeLogbookAdapter

        fake_adapter = FakeLogbookAdapter()
        fake_adapter.configure_dedup_miss()

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            openmemory_client=fake_client,
            logbook_adapter=fake_adapter,
        )

        with patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine:
            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = "team:fake_project"
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space="team:fake_project",
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 验证结果
            assert result.ok is True
            assert result.memory_id == "fake_mem_id"

            # 验证 fake_client 被调用
            assert len(fake_client.store_calls) == 1
            assert fake_client.store_calls[0]["content"] == payload_md

    @pytest.mark.asyncio
    async def test_fake_client_connection_error(self):
        """
        测试 Fake client 连接错误模式
        """
        payload_md = "# Test fake connection error"

        fake_config = FakeGatewayConfig()

        fake_db = FakeLogbookDatabase()
        fake_db.configure_settings(team_write_enabled=True)
        fake_db.configure_outbox_success(start_id=999)

        # 需要转换 fake 异常为真实异常
        from engram.gateway.openmemory_client import OpenMemoryConnectionError

        mock_client = MagicMock()
        mock_client.store.side_effect = OpenMemoryConnectionError("Fake 连接超时")

        # 创建 mock logbook_adapter
        from tests.gateway.fakes import FakeLogbookAdapter

        fake_adapter = FakeLogbookAdapter()
        fake_adapter.configure_dedup_miss()

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            openmemory_client=mock_client,
            logbook_adapter=fake_adapter,
        )

        with patch(f"{HANDLER_MODULE}.create_engine_from_settings") as mock_engine:
            # 模拟策略引擎
            from engram.gateway.policy import PolicyAction

            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "policy_passed"
            mock_decision.final_space = "team:test"
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space="team:test",
                correlation_id=_test_correlation_id(),
                deps=deps,
            )

            # 验证 deferred 响应
            assert result.action == "deferred"
            assert result.outbox_id == 999


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
