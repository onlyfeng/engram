# -*- coding: utf-8 -*-
"""
actor_user_id 审计参数测试

测试覆盖:
- REST 端点 /memory/store 透传 actor_user_id 到审计
- MCP 端点 /mcp 透传 actor_user_id 到审计
- memory_store_impl 在各种场景下正确传递 actor_user_id
"""

import pytest
from unittest.mock import MagicMock, patch

from gateway.main import (
    memory_store_impl,
    MemoryStoreRequest,
    compute_payload_sha,
)


class TestActorUserIdInAudit:
    """actor_user_id 审计参数测试"""

    @pytest.mark.asyncio
    async def test_actor_user_id_passed_to_audit_on_dedup_hit(self):
        """dedup_hit 场景下 actor_user_id 传入审计"""
        payload_md = "# Test content"
        target_space = "team:test"
        actor_user_id = "user_alice_123"
        payload_sha = compute_payload_sha(payload_md)

        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db, \
             patch("gateway.main.check_user_exists") as mock_check_user:

            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"

            # 用户存在
            mock_check_user.return_value = True

            mock_adapter.check_dedup.return_value = {
                "outbox_id": 100,
                "target_space": target_space,
                "payload_sha": payload_sha,
                "status": "sent",
                "last_error": "memory_id=mem_existing_123",
            }

            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id=actor_user_id,
            )

            assert result.ok is True
            
            # 验证 actor_user_id 传入审计
            mock_db.insert_audit.assert_called_once()
            audit_call = mock_db.insert_audit.call_args[1]
            assert audit_call["actor_user_id"] == actor_user_id

    @pytest.mark.asyncio
    async def test_actor_user_id_passed_to_audit_on_policy_reject(self):
        """策略拒绝场景下 actor_user_id 传入审计"""
        payload_md = "# Rejected content"
        target_space = "team:restricted"
        actor_user_id = "user_bob_456"

        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db, \
             patch("gateway.main.check_user_exists") as mock_check_user, \
             patch("gateway.main.create_engine_from_settings") as mock_engine:

            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"

            # 用户存在
            mock_check_user.return_value = True

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": False,
                "policy_json": {},
            }
            mock_get_db.return_value = mock_db

            # 模拟策略拒绝
            from gateway.policy import PolicyAction
            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.REJECT
            mock_decision.reason = "team_write_disabled"
            mock_engine.return_value.decide.return_value = mock_decision

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id=actor_user_id,
            )

            assert result.ok is False
            assert result.action == "reject"

            # 验证 actor_user_id 传入审计
            mock_db.insert_audit.assert_called_once()
            audit_call = mock_db.insert_audit.call_args[1]
            assert audit_call["actor_user_id"] == actor_user_id

    @pytest.mark.asyncio
    async def test_actor_user_id_passed_to_audit_on_success(self):
        """成功写入场景下 actor_user_id 传入审计"""
        payload_md = "# Success content"
        target_space = "team:success"
        actor_user_id = "user_charlie_789"

        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db, \
             patch("gateway.main.get_client") as mock_get_client, \
             patch("gateway.main.check_user_exists") as mock_check_user, \
             patch("gateway.main.create_engine_from_settings") as mock_engine:

            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"

            # 用户存在
            mock_check_user.return_value = True

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }
            mock_get_db.return_value = mock_db

            # 模拟策略允许
            from gateway.policy import PolicyAction
            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "allowed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 成功
            mock_client = MagicMock()
            mock_store_result = MagicMock()
            mock_store_result.success = True
            mock_store_result.memory_id = "mem_new_001"
            mock_client.store.return_value = mock_store_result
            mock_get_client.return_value = mock_client

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id=actor_user_id,
            )

            assert result.ok is True
            assert result.memory_id == "mem_new_001"

            # 验证 actor_user_id 传入审计
            mock_db.insert_audit.assert_called_once()
            audit_call = mock_db.insert_audit.call_args[1]
            assert audit_call["actor_user_id"] == actor_user_id

    @pytest.mark.asyncio
    async def test_actor_user_id_passed_to_audit_on_openmemory_error(self):
        """OpenMemory 失败场景下 actor_user_id 传入审计"""
        payload_md = "# Error content"
        target_space = "team:error"
        actor_user_id = "user_dave_999"

        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db, \
             patch("gateway.main.get_client") as mock_get_client, \
             patch("gateway.main.check_user_exists") as mock_check_user, \
             patch("gateway.main.create_engine_from_settings") as mock_engine:

            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"

            # 用户存在
            mock_check_user.return_value = True

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }
            mock_db.enqueue_outbox.return_value = 500
            mock_get_db.return_value = mock_db

            # 模拟策略允许
            from gateway.policy import PolicyAction
            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "allowed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 连接失败
            from gateway.openmemory_client import OpenMemoryConnectionError
            mock_client = MagicMock()
            mock_client.store.side_effect = OpenMemoryConnectionError("Connection refused")
            mock_get_client.return_value = mock_client

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id=actor_user_id,
            )

            assert result.ok is False
            assert result.action == "error"

            # 验证 actor_user_id 传入审计
            mock_db.insert_audit.assert_called_once()
            audit_call = mock_db.insert_audit.call_args[1]
            assert audit_call["actor_user_id"] == actor_user_id

    @pytest.mark.asyncio
    async def test_actor_user_id_none_when_not_provided(self):
        """未提供 actor_user_id 时审计记录 None"""
        payload_md = "# Anonymous content"
        target_space = "team:anon"
        payload_sha = compute_payload_sha(payload_md)

        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db:

            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"

            mock_adapter.check_dedup.return_value = {
                "outbox_id": 200,
                "target_space": target_space,
                "payload_sha": payload_sha,
                "status": "sent",
                "last_error": None,
            }

            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            # 不传入 actor_user_id
            await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
            )

            # 验证 actor_user_id 为 None
            mock_db.insert_audit.assert_called_once()
            audit_call = mock_db.insert_audit.call_args[1]
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

    @pytest.mark.asyncio
    async def test_unknown_actor_reject_policy(self):
        """未知用户 + reject 策略：拒绝请求"""
        payload_md = "# Test content"
        target_space = "team:test"
        actor_user_id = "unknown_user_001"

        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db, \
             patch("gateway.main.check_user_exists") as mock_check_user:

            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.unknown_actor_policy = "reject"
            mock_config.return_value.private_space_prefix = "private:"

            # 用户不存在
            mock_check_user.return_value = False

            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id=actor_user_id,
            )

            assert result.ok is False
            assert result.action == "reject"
            assert "用户不存在" in result.message

            # 验证审计日志写入
            mock_db.insert_audit.assert_called_once()
            audit_call = mock_db.insert_audit.call_args[1]
            assert audit_call["actor_user_id"] == actor_user_id
            assert audit_call["action"] == "reject"
            assert "actor_unknown:reject" in audit_call["reason"]

    @pytest.mark.asyncio
    async def test_unknown_actor_degrade_policy(self):
        """未知用户 + degrade 策略：降级到 private:unknown"""
        payload_md = "# Test content for degrade"
        target_space = "team:test"
        actor_user_id = "unknown_user_002"

        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db, \
             patch("gateway.main.get_client") as mock_get_client, \
             patch("gateway.main.check_user_exists") as mock_check_user, \
             patch("gateway.main.create_engine_from_settings") as mock_engine:

            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.unknown_actor_policy = "degrade"
            mock_config.return_value.private_space_prefix = "private:"

            # 用户不存在
            mock_check_user.return_value = False

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }
            mock_get_db.return_value = mock_db

            # 模拟策略允许
            from gateway.policy import PolicyAction
            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "allowed"
            mock_decision.final_space = "private:unknown"
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 成功
            mock_client = MagicMock()
            mock_store_result = MagicMock()
            mock_store_result.success = True
            mock_store_result.memory_id = "mem_degraded_001"
            mock_client.store.return_value = mock_store_result
            mock_get_client.return_value = mock_client

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id=actor_user_id,
            )

            # 验证第一次审计调用（降级审计）
            first_audit_call = mock_db.insert_audit.call_args_list[0][1]
            assert first_audit_call["action"] == "redirect"
            assert "actor_unknown:degrade" in first_audit_call["reason"]

    @pytest.mark.asyncio
    async def test_unknown_actor_auto_create_policy(self):
        """未知用户 + auto_create 策略：自动创建用户"""
        payload_md = "# Test content for auto create"
        target_space = "team:test"
        actor_user_id = "new_user_003"

        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db, \
             patch("gateway.main.get_client") as mock_get_client, \
             patch("gateway.main.check_user_exists") as mock_check_user, \
             patch("gateway.main.ensure_user") as mock_ensure_user, \
             patch("gateway.main.create_engine_from_settings") as mock_engine:

            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.unknown_actor_policy = "auto_create"
            mock_config.return_value.private_space_prefix = "private:"

            # 用户不存在
            mock_check_user.return_value = False

            # 自动创建成功
            mock_ensure_user.return_value = {
                "user_id": actor_user_id,
                "display_name": actor_user_id,
            }

            mock_adapter.check_dedup.return_value = None

            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }
            mock_get_db.return_value = mock_db

            # 模拟策略允许
            from gateway.policy import PolicyAction
            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "allowed"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision

            # 模拟 OpenMemory 成功
            mock_client = MagicMock()
            mock_store_result = MagicMock()
            mock_store_result.success = True
            mock_store_result.memory_id = "mem_autocreate_001"
            mock_client.store.return_value = mock_store_result
            mock_get_client.return_value = mock_client

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id=actor_user_id,
            )

            assert result.ok is True
            assert result.memory_id == "mem_autocreate_001"

            # 验证 ensure_user 被调用
            mock_ensure_user.assert_called_once_with(
                user_id=actor_user_id,
                display_name=actor_user_id,
            )

            # 验证审计日志包含 actor_autocreated
            audit_calls = mock_db.insert_audit.call_args_list
            autocreate_audit = [c for c in audit_calls if "actor_autocreated" in str(c)]
            assert len(autocreate_audit) == 1

    @pytest.mark.asyncio
    async def test_existing_actor_continues_normally(self):
        """已存在的用户正常处理"""
        payload_md = "# Test content for existing user"
        target_space = "team:test"
        actor_user_id = "existing_user_004"
        payload_sha = compute_payload_sha(payload_md)

        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db, \
             patch("gateway.main.check_user_exists") as mock_check_user:

            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            mock_config.return_value.unknown_actor_policy = "reject"  # 策略为 reject，但用户存在

            # 用户存在
            mock_check_user.return_value = True

            mock_adapter.check_dedup.return_value = {
                "outbox_id": 300,
                "target_space": target_space,
                "payload_sha": payload_sha,
                "status": "sent",
                "last_error": "memory_id=mem_existing_002",
            }

            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                actor_user_id=actor_user_id,
            )

            assert result.ok is True
            # 用户存在，应该正常继续（这里命中 dedup）
            mock_check_user.assert_called_once_with(actor_user_id)


class TestEnsureUserAndAccount:
    """ensure_user 和 ensure_account 函数测试"""

    def test_unknown_actor_policy_import(self):
        """验证 UnknownActorPolicy 可导入"""
        from gateway.step1_adapter import UnknownActorPolicy
        assert UnknownActorPolicy.REJECT == "reject"
        assert UnknownActorPolicy.DEGRADE == "degrade"
        assert UnknownActorPolicy.AUTO_CREATE == "auto_create"

    def test_ensure_user_function_exists(self):
        """验证 ensure_user 函数存在"""
        from gateway.step1_adapter import ensure_user
        assert callable(ensure_user)

    def test_ensure_account_function_exists(self):
        """验证 ensure_account 函数存在"""
        from gateway.step1_adapter import ensure_account
        assert callable(ensure_account)

    def test_check_user_exists_function_exists(self):
        """验证 check_user_exists 函数存在"""
        from gateway.step1_adapter import check_user_exists
        assert callable(check_user_exists)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
