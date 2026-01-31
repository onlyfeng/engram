# -*- coding: utf-8 -*-
"""
actor_user_id 审计参数测试

测试覆盖:
- REST 端点 /memory/store 透传 actor_user_id 到审计
- MCP 端点 /mcp 透传 actor_user_id 到审计
- memory_store_impl 在各种场景下正确传递 actor_user_id

================================================================================
依赖注入说明 (v1.0):
================================================================================

本测试使用 GatewayDeps.for_testing() 进行依赖注入，替代旧的 patch 方式。

使用方式:
    deps = GatewayDeps.for_testing(
        config=fake_config,
        db=fake_db,
        logbook_adapter=fake_adapter,
        openmemory_client=fake_client,
    )
    result = await memory_store_impl(
        payload_md=...,
        correlation_id=...,
        deps=deps,
        actor_user_id=...,
    )
"""

from unittest.mock import MagicMock

import pytest

from engram.gateway.app import MemoryStoreRequest
from engram.gateway.di import GatewayDeps
from engram.gateway.handlers.memory_store import memory_store_impl
from engram.gateway.services.hash_utils import compute_payload_sha

# 导入 Fake 依赖
from tests.gateway.fakes import (
    FakeGatewayConfig,
    FakeLogbookAdapter,
    FakeLogbookDatabase,
    FakeOpenMemoryClient,
)


def _test_correlation_id():
    """生成测试用的 correlation_id"""
    import secrets

    return f"corr-{secrets.token_hex(8)}"


class TestActorUserIdInAudit:
    """actor_user_id 审计参数测试"""

    @pytest.mark.asyncio
    async def test_actor_user_id_passed_to_audit_on_dedup_hit(self):
        """dedup_hit 场景下 actor_user_id 传入审计"""
        payload_md = "# Test content"
        target_space = "team:test"
        actor_user_id = "user_alice_123"
        payload_sha = compute_payload_sha(payload_md)

        fake_config = FakeGatewayConfig()
        fake_db = FakeLogbookDatabase()
        fake_adapter = FakeLogbookAdapter()
        fake_client = FakeOpenMemoryClient()

        # 配置用户存在
        fake_adapter.configure_user_exists(True)

        # 配置 dedup hit
        fake_adapter.configure_dedup_hit(
            outbox_id=100,
            target_space=target_space,
            payload_sha=payload_sha,
            status="sent",
            memory_id="mem_existing_123",
        )

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            logbook_adapter=fake_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md=payload_md,
            correlation_id=_test_correlation_id(),
            deps=deps,
            target_space=target_space,
            actor_user_id=actor_user_id,
        )

        assert result.ok is True

        # 验证 actor_user_id 传入审计
        assert len(fake_db.audit_calls) == 1
        audit_call = fake_db.audit_calls[0]
        assert audit_call["actor_user_id"] == actor_user_id

    @pytest.mark.asyncio
    async def test_actor_user_id_passed_to_audit_on_policy_redirect(self):
        """策略重定向场景下 actor_user_id 传入审计

        当 team_write_enabled=False 时，策略会 redirect 到私有空间而非 reject
        """
        payload_md = "# Redirected content"
        target_space = "team:restricted"
        actor_user_id = "user_bob_456"

        fake_config = FakeGatewayConfig()
        fake_db = FakeLogbookDatabase()
        # 配置 settings 禁用 team_write -> 触发 redirect
        fake_db.configure_settings(team_write_enabled=False, policy_json={})
        fake_adapter = FakeLogbookAdapter()
        # 配置用户存在
        fake_adapter.configure_user_exists(True)
        fake_adapter.configure_dedup_miss()
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="mem_redirected_001")

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            logbook_adapter=fake_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md=payload_md,
            correlation_id=_test_correlation_id(),
            deps=deps,
            target_space=target_space,
            actor_user_id=actor_user_id,
        )

        assert result.ok is True
        assert result.action == "redirect"

        # 验证 actor_user_id 传入审计
        assert len(fake_db.audit_calls) >= 1
        audit_call = fake_db.audit_calls[-1]  # 最后一次审计调用
        assert audit_call["actor_user_id"] == actor_user_id

    @pytest.mark.asyncio
    async def test_actor_user_id_passed_to_audit_on_success(self):
        """成功写入场景下 actor_user_id 传入审计"""
        payload_md = "# Success content"
        target_space = "team:success"
        actor_user_id = "user_charlie_789"

        fake_config = FakeGatewayConfig()
        fake_db = FakeLogbookDatabase()
        # 配置 settings 启用 team_write
        fake_db.configure_settings(team_write_enabled=True, policy_json={})
        fake_adapter = FakeLogbookAdapter()
        # 配置用户存在
        fake_adapter.configure_user_exists(True)
        fake_adapter.configure_dedup_miss()
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="mem_new_001")

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            logbook_adapter=fake_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md=payload_md,
            correlation_id=_test_correlation_id(),
            deps=deps,
            target_space=target_space,
            actor_user_id=actor_user_id,
        )

        assert result.ok is True
        assert result.memory_id == "mem_new_001"

        # 验证 actor_user_id 传入审计
        assert len(fake_db.audit_calls) >= 1
        audit_call = fake_db.audit_calls[-1]  # 最后一次审计调用
        assert audit_call["actor_user_id"] == actor_user_id

    @pytest.mark.asyncio
    async def test_actor_user_id_passed_to_audit_on_openmemory_error(self):
        """OpenMemory 失败场景下 actor_user_id 传入审计"""
        payload_md = "# Error content"
        target_space = "team:error"
        actor_user_id = "user_dave_999"

        fake_config = FakeGatewayConfig()
        fake_db = FakeLogbookDatabase()
        fake_db.configure_settings(team_write_enabled=True, policy_json={})
        fake_db.configure_outbox_success(start_id=500)
        fake_adapter = FakeLogbookAdapter()
        # 配置用户存在
        fake_adapter.configure_user_exists(True)
        fake_adapter.configure_dedup_miss()

        # 模拟 OpenMemory 连接失败
        from engram.gateway.openmemory_client import OpenMemoryConnectionError

        fake_client = MagicMock()
        fake_client.store.side_effect = OpenMemoryConnectionError("Connection refused")

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            logbook_adapter=fake_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md=payload_md,
            correlation_id=_test_correlation_id(),
            deps=deps,
            target_space=target_space,
            actor_user_id=actor_user_id,
        )

        assert result.ok is False
        assert result.action == "deferred"

        # 验证 actor_user_id 传入审计
        assert len(fake_db.audit_calls) >= 1
        audit_call = fake_db.audit_calls[-1]
        assert audit_call["actor_user_id"] == actor_user_id

    @pytest.mark.asyncio
    async def test_actor_user_id_none_when_not_provided(self):
        """未提供 actor_user_id 时审计记录 None"""
        payload_md = "# Anonymous content"
        target_space = "team:anon"
        payload_sha = compute_payload_sha(payload_md)

        fake_config = FakeGatewayConfig()
        fake_db = FakeLogbookDatabase()
        fake_adapter = FakeLogbookAdapter()
        fake_client = FakeOpenMemoryClient()

        # 配置 dedup hit
        fake_adapter.configure_dedup_hit(
            outbox_id=200,
            target_space=target_space,
            payload_sha=payload_sha,
            status="sent",
            memory_id=None,
        )

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            logbook_adapter=fake_adapter,
            openmemory_client=fake_client,
        )

        # 不传入 actor_user_id
        await memory_store_impl(
            payload_md=payload_md,
            correlation_id=_test_correlation_id(),
            deps=deps,
            target_space=target_space,
        )

        # 验证 actor_user_id 为 None
        assert len(fake_db.audit_calls) >= 1
        audit_call = fake_db.audit_calls[0]
        assert audit_call["actor_user_id"] is None


class TestMemoryStoreRequestModel:
    """MemoryStoreRequest 模型测试"""

    def test_actor_user_id_field_exists(self):
        """验证 actor_user_id 字段存在"""
        request = MemoryStoreRequest(
            payload_md="# Test",
            actor_user_id="user_test",
        )
        assert request.actor_user_id == "user_test"

    def test_actor_user_id_optional(self):
        """验证 actor_user_id 可选"""
        request = MemoryStoreRequest(
            payload_md="# Test",
        )
        assert request.actor_user_id is None

    def test_full_request_with_actor_user_id(self):
        """验证完整请求包含 actor_user_id"""
        request = MemoryStoreRequest(
            payload_md="# Full test",
            target_space="team:full",
            meta_json={"key": "value"},
            kind="FACT",
            evidence_refs=["ref1"],
            is_bulk=False,
            item_id=123,
            actor_user_id="user_full",
        )
        assert request.payload_md == "# Full test"
        assert request.target_space == "team:full"
        assert request.actor_user_id == "user_full"


class TestActorUserValidation:
    """actor_user_id 校验测试"""

    def _assert_audit_has_policy_validation_substructures(
        self, evidence_refs_json: dict, allow_none_values: bool = True
    ):
        """
        断言 evidence_refs_json.gateway_event 包含 policy 和 validation 子结构

        v1.1 契约要求：所有分支的审计事件必须包含这些子结构
        """
        gateway_event = evidence_refs_json.get("gateway_event", {})

        # 验证 policy 子结构存在
        assert "policy" in gateway_event, "gateway_event 必须包含 policy 子结构"
        policy = gateway_event["policy"]
        for field in ["mode", "mode_reason", "policy_version", "is_pointerized", "policy_source"]:
            assert field in policy, f"policy 必须包含 {field} 字段"

        # 验证 validation 子结构存在
        assert "validation" in gateway_event, "gateway_event 必须包含 validation 子结构"
        validation = gateway_event["validation"]
        for field in ["validate_refs_effective", "validate_refs_reason", "evidence_validation"]:
            assert field in validation, f"validation 必须包含 {field} 字段"

        # actor 分支发生在策略评估之前，mode_reason 应该说明原因
        if allow_none_values:
            assert "actor_validation" in policy.get("mode_reason", "") or "before" in policy.get(
                "mode_reason", ""
            ), "actor 分支的 policy.mode_reason 应说明是 actor_validation 阶段"

    @pytest.mark.asyncio
    async def test_unknown_actor_reject_policy(self):
        """未知用户 + reject 策略：拒绝请求"""
        payload_md = "# Test content"
        target_space = "team:test"
        actor_user_id = "unknown_user_001"

        fake_config = FakeGatewayConfig()
        fake_config.unknown_actor_policy = "reject"
        fake_db = FakeLogbookDatabase()
        fake_adapter = FakeLogbookAdapter()
        # 配置用户不存在
        fake_adapter.configure_user_exists(False)
        fake_client = FakeOpenMemoryClient()

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            logbook_adapter=fake_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md=payload_md,
            correlation_id=_test_correlation_id(),
            deps=deps,
            target_space=target_space,
            actor_user_id=actor_user_id,
        )

        assert result.ok is False
        assert result.action == "reject"
        assert "用户不存在" in result.message

        # 验证审计日志写入
        assert len(fake_db.audit_calls) >= 1
        audit_call = fake_db.audit_calls[0]
        assert audit_call["actor_user_id"] == actor_user_id
        assert audit_call["action"] == "reject"
        assert "actor_unknown:reject" in audit_call["reason"]

        # v1.1 契约：验证 policy/validation 子结构存在
        evidence_refs_json = audit_call.get("evidence_refs_json", {})
        self._assert_audit_has_policy_validation_substructures(evidence_refs_json)

    @pytest.mark.asyncio
    async def test_unknown_actor_degrade_policy(self):
        """未知用户 + degrade 策略：降级到 private:unknown"""
        payload_md = "# Test content for degrade"
        target_space = "team:test"
        actor_user_id = "unknown_user_002"

        fake_config = FakeGatewayConfig()
        fake_config.unknown_actor_policy = "degrade"
        fake_db = FakeLogbookDatabase()
        fake_db.configure_settings(team_write_enabled=True, policy_json={})
        fake_adapter = FakeLogbookAdapter()
        # 配置用户不存在
        fake_adapter.configure_user_exists(False)
        fake_adapter.configure_dedup_miss()
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="mem_degraded_001")

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            logbook_adapter=fake_adapter,
            openmemory_client=fake_client,
        )

        await memory_store_impl(
            payload_md=payload_md,
            correlation_id=_test_correlation_id(),
            deps=deps,
            target_space=target_space,
            actor_user_id=actor_user_id,
        )

        # 验证第一次审计调用（降级审计）
        assert len(fake_db.audit_calls) >= 1
        first_audit_call = fake_db.audit_calls[0]
        assert first_audit_call["action"] == "redirect"
        assert "actor_unknown:degrade" in first_audit_call["reason"]

        # v1.1 契约：验证 policy/validation 子结构存在
        evidence_refs_json = first_audit_call.get("evidence_refs_json", {})
        self._assert_audit_has_policy_validation_substructures(evidence_refs_json)

    @pytest.mark.asyncio
    async def test_unknown_actor_auto_create_policy(self):
        """未知用户 + auto_create 策略：自动创建用户"""
        payload_md = "# Test content for auto create"
        target_space = "team:test"
        actor_user_id = "new_user_003"

        fake_config = FakeGatewayConfig()
        fake_config.unknown_actor_policy = "auto_create"
        fake_db = FakeLogbookDatabase()
        fake_db.configure_settings(team_write_enabled=True, policy_json={})
        fake_adapter = FakeLogbookAdapter()
        # 配置用户不存在
        fake_adapter.configure_user_exists(False)
        fake_adapter.configure_dedup_miss()
        fake_client = FakeOpenMemoryClient()
        fake_client.configure_store_success(memory_id="mem_autocreate_001")

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            logbook_adapter=fake_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md=payload_md,
            correlation_id=_test_correlation_id(),
            deps=deps,
            target_space=target_space,
            actor_user_id=actor_user_id,
        )

        assert result.ok is True
        assert result.memory_id == "mem_autocreate_001"

        # 验证 ensure_user 被调用
        assert len(fake_adapter.ensure_user_calls) == 1
        ensure_call = fake_adapter.ensure_user_calls[0]
        assert ensure_call["user_id"] == actor_user_id
        assert ensure_call["display_name"] == actor_user_id

        # 验证审计日志包含 actor_autocreated
        audit_calls = fake_db.audit_calls
        autocreate_audit = [c for c in audit_calls if "actor_autocreated" in str(c)]
        assert len(autocreate_audit) == 1

        # v1.1 契约：验证 actor_autocreated 审计的 policy/validation 子结构
        autocreate_call = autocreate_audit[0]
        evidence_refs_json = autocreate_call.get("evidence_refs_json", {})
        self._assert_audit_has_policy_validation_substructures(evidence_refs_json)

    @pytest.mark.asyncio
    async def test_existing_actor_continues_normally(self):
        """已存在的用户正常处理"""
        payload_md = "# Test content for existing user"
        target_space = "team:test"
        actor_user_id = "existing_user_004"
        payload_sha = compute_payload_sha(payload_md)

        fake_config = FakeGatewayConfig()
        fake_config.unknown_actor_policy = "reject"  # 策略为 reject，但用户存在
        fake_db = FakeLogbookDatabase()
        fake_adapter = FakeLogbookAdapter()
        # 配置用户存在
        fake_adapter.configure_user_exists(True)
        fake_client = FakeOpenMemoryClient()

        # 配置 dedup hit（用户存在时正常命中 dedup）
        fake_adapter.configure_dedup_hit(
            outbox_id=300,
            target_space=target_space,
            payload_sha=payload_sha,
            status="sent",
            memory_id="mem_existing_002",
        )

        deps = GatewayDeps.for_testing(
            config=fake_config,
            db=fake_db,
            logbook_adapter=fake_adapter,
            openmemory_client=fake_client,
        )

        result = await memory_store_impl(
            payload_md=payload_md,
            correlation_id=_test_correlation_id(),
            deps=deps,
            target_space=target_space,
            actor_user_id=actor_user_id,
        )

        assert result.ok is True
        # 用户存在，应该正常继续（这里命中 dedup）
        assert len(fake_adapter.check_user_calls) == 1
        assert fake_adapter.check_user_calls[0] == actor_user_id


class TestEnsureUserAndAccount:
    """ensure_user 和 ensure_account 函数测试"""

    def test_unknown_actor_policy_import(self):
        """验证 UnknownActorPolicy 可导入"""
        from engram.gateway.logbook_adapter import UnknownActorPolicy

        assert UnknownActorPolicy.REJECT == "reject"
        assert UnknownActorPolicy.DEGRADE == "degrade"
        assert UnknownActorPolicy.AUTO_CREATE == "auto_create"

    def test_ensure_user_function_exists(self):
        """验证 ensure_user 函数存在"""
        from engram.gateway.logbook_adapter import ensure_user

        assert callable(ensure_user)

    def test_ensure_account_function_exists(self):
        """验证 ensure_account 函数存在"""
        from engram.gateway.logbook_adapter import ensure_account

        assert callable(ensure_account)

    def test_check_user_exists_function_exists(self):
        """验证 check_user_exists 函数存在"""
        from engram.gateway.logbook_adapter import check_user_exists

        assert callable(check_user_exists)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
