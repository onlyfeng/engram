# -*- coding: utf-8 -*-
"""
memory_store_impl Dedupe 测试

测试覆盖:
- 写入前 dedupe check
- dedup_hit 时直接返回并写入审计
- 无重复时继续正常流程
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from gateway.main import memory_store_impl, compute_payload_sha


class TestMemoryStoreDedup:
    """memory_store_impl dedupe 测试"""

    @pytest.mark.asyncio
    async def test_dedup_hit_returns_early(self):
        """存在已成功写入的记录时直接返回"""
        payload_md = "# Test content for dedup"
        target_space = "team:test"
        payload_sha = compute_payload_sha(payload_md)
        
        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db, \
             patch("gateway.main.get_client") as mock_get_client:
            
            # 配置 mock
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            
            # 模拟 check_dedup 返回已存在的记录
            mock_adapter.check_dedup.return_value = {
                "outbox_id": 100,
                "target_space": target_space,
                "payload_sha": payload_sha,
                "status": "sent",
                "last_error": "memory_id=mem_existing_123",
            }
            
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            
            # 执行
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
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
            assert evidence["extra"]["correlation_id"].startswith("corr-"), "correlation_id 应以 corr- 开头"
            assert evidence.get("source") == "gateway", "source 应为 gateway"
            
            # 验证 OpenMemory 未被调用
            mock_get_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_hit_audit_contains_original_outbox_id(self):
        """dedup_hit 审计日志包含原始 outbox_id"""
        payload_md = "# Another test"
        target_space = "private:alice"
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
            
            await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
                evidence_refs=["ref1", "ref2"],
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
        
        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db, \
             patch("gateway.main.get_client") as mock_get_client, \
             patch("gateway.main.create_engine_from_settings") as mock_engine:
            
            mock_config.return_value.default_team_space = "team:default"
            mock_config.return_value.project_key = "test_project"
            
            # 模拟 check_dedup 返回 None（无重复）
            mock_adapter.check_dedup.return_value = None
            
            mock_db = MagicMock()
            mock_db.get_or_create_settings.return_value = {
                "team_write_enabled": True,
                "policy_json": {},
            }
            mock_get_db.return_value = mock_db
            
            # 模拟策略引擎
            from gateway.policy import PolicyAction
            mock_decision = MagicMock()
            mock_decision.action = PolicyAction.ALLOW
            mock_decision.reason = "test"
            mock_decision.final_space = target_space
            mock_engine.return_value.decide.return_value = mock_decision
            
            # 模拟 OpenMemory 客户端
            mock_client = MagicMock()
            mock_store_result = MagicMock()
            mock_store_result.success = True
            mock_store_result.memory_id = "mem_new_456"
            mock_client.store.return_value = mock_store_result
            mock_get_client.return_value = mock_client
            
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=target_space,
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
        
        with patch("gateway.main.get_config") as mock_config, \
             patch("gateway.main.step1_adapter") as mock_adapter, \
             patch("gateway.main.get_db") as mock_get_db:
            
            mock_config.return_value.default_team_space = "team:default_project"
            mock_config.return_value.project_key = "test_project"
            
            mock_adapter.check_dedup.return_value = {
                "outbox_id": 300,
                "target_space": "team:default_project",
                "payload_sha": compute_payload_sha(payload_md),
                "status": "sent",
                "last_error": None,
            }
            
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            
            # 不传入 target_space，使用默认值
            result = await memory_store_impl(
                payload_md=payload_md,
                target_space=None,
            )
            
            # 验证使用默认 target_space 进行 check_dedup
            mock_adapter.check_dedup.assert_called_once()
            call_kwargs = mock_adapter.check_dedup.call_args[1]
            assert call_kwargs["target_space"] == "team:default_project"
            
            assert result.ok is True
            assert result.action == "allow"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
