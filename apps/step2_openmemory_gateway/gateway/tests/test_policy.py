"""
policy 模块单元测试

测试覆盖:
- PolicyEngine 各分支策略决策
- decide_write 便捷函数
- 典型场景: payload 长度、证据缺失、kind 不允许、bulk 模式
"""

import pytest
from gateway.policy import (
    PolicyAction,
    PolicyDecision,
    PolicyEngine,
    create_engine_from_settings,
    decide_write,
)


class TestPolicyEngine:
    """PolicyEngine 策略引擎测试"""

    def test_private_space_always_allowed(self):
        """私有空间写入始终允许"""
        engine = PolicyEngine(team_write_enabled=False)
        decision = engine.decide(
            target_space="private:alice",
            actor_user_id="alice",
            payload_md="some content",
        )
        assert decision.action == PolicyAction.ALLOW
        assert decision.reason == "private_space"
        assert decision.final_space == "private:alice"

    def test_team_write_disabled_redirects(self):
        """团队写入禁用时重定向到私有空间"""
        engine = PolicyEngine(team_write_enabled=False)
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "team_write_disabled"
        assert decision.final_space == "private:alice"

    def test_user_not_in_allowlist_redirects(self):
        """用户不在白名单时重定向"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"allowlist_users": ["bob", "charlie"]},
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="short",
            kind="PROCEDURE",
            evidence_refs=["ref1"],
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "user_not_in_allowlist"

    def test_user_in_allowlist_passes(self):
        """用户在白名单时通过"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "allowlist_users": ["alice", "bob"],
                "require_evidence": False,  # 关闭证据检查以简化测试
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="short",
            kind="PROCEDURE",
        )
        assert decision.action == PolicyAction.ALLOW
        assert decision.reason == "policy_passed"

    def test_kind_not_allowed_redirects(self):
        """知识类型不允许时重定向"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "allowed_kinds": ["PROCEDURE", "PITFALL"],
                "require_evidence": False,
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="short",
            kind="FACT",  # FACT 不在允许列表中
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "kind_not_allowed:FACT"

    def test_kind_allowed_passes(self):
        """知识类型允许时通过"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "allowed_kinds": ["PROCEDURE", "PITFALL"],
                "require_evidence": False,
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="short",
            kind="PROCEDURE",
        )
        assert decision.action == PolicyAction.ALLOW

    def test_missing_evidence_redirects(self):
        """缺少证据链时重定向"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "require_evidence": True,
                "allowed_kinds": [],  # 不限制 kind
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="short",
            evidence_refs=None,  # 无证据
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "missing_evidence"

    def test_empty_evidence_redirects(self):
        """空证据列表时重定向"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"require_evidence": True, "allowed_kinds": []},
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="short",
            evidence_refs=[],  # 空列表
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "missing_evidence"

    def test_evidence_provided_passes(self):
        """提供证据时通过"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"require_evidence": True, "allowed_kinds": []},
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="short",
            evidence_refs=["commit:abc123", "file:readme.md"],
        )
        assert decision.action == PolicyAction.ALLOW

    def test_exceeds_max_chars_redirects(self):
        """超过最大字符数时重定向"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "max_chars": 100,
                "require_evidence": False,
                "allowed_kinds": [],
            },
        )
        long_content = "x" * 150  # 超过 100 字符
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md=long_content,
        )
        assert decision.action == PolicyAction.REDIRECT
        assert "exceeds_max_chars" in decision.reason
        assert "150>100" in decision.reason

    def test_within_max_chars_passes(self):
        """在最大字符数内时通过"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "max_chars": 100,
                "require_evidence": False,
                "allowed_kinds": [],
            },
        )
        short_content = "x" * 50  # 50 字符
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md=short_content,
        )
        assert decision.action == PolicyAction.ALLOW

    def test_bulk_mode_very_short_too_long_redirects(self):
        """bulk 模式下内容过长时重定向"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "bulk_mode": "very_short",
                "require_evidence": False,
                "allowed_kinds": [],
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="x" * 250,  # 超过 200 字符
            is_bulk=True,
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "bulk_too_long"

    def test_bulk_mode_very_short_within_limit_passes(self):
        """bulk 模式下内容短时通过"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "bulk_mode": "very_short",
                "require_evidence": False,
                "allowed_kinds": [],
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="x" * 100,  # 100 字符，< 200
            is_bulk=True,
        )
        assert decision.action == PolicyAction.ALLOW

    def test_bulk_mode_reject_redirects(self):
        """bulk_mode=reject 时批量提交重定向"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "bulk_mode": "reject",
                "require_evidence": False,
                "allowed_kinds": [],
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="short",
            is_bulk=True,
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "bulk_not_allowed"

    def test_bulk_mode_allow_passes(self):
        """bulk_mode=allow 时批量提交通过"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "bulk_mode": "allow",  # 完全允许 bulk
                "require_evidence": False,
                "allowed_kinds": [],
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="any length content here",
            is_bulk=True,
        )
        assert decision.action == PolicyAction.ALLOW

    def test_unknown_space_type_rejects(self):
        """未知空间类型拒绝"""
        engine = PolicyEngine(team_write_enabled=True)
        decision = engine.decide(
            target_space="unknown:space",
            actor_user_id="alice",
            payload_md="content",
        )
        assert decision.action == PolicyAction.REJECT
        assert decision.reason == "unknown_space_type"

    def test_org_space_follows_team_policy(self):
        """org 空间遵循团队策略"""
        engine = PolicyEngine(team_write_enabled=False)
        decision = engine.decide(
            target_space="org:shared",
            actor_user_id="alice",
            payload_md="content",
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "team_write_disabled"


class TestPolicyDecision:
    """PolicyDecision 数据结构测试"""

    def test_to_dict(self):
        """to_dict 序列化测试"""
        decision = PolicyDecision(
            action=PolicyAction.ALLOW,
            reason="test_reason",
            original_space="team:project",
            final_space="team:project",
        )
        result = decision.to_dict()
        assert result == {
            "action": "allow",
            "reason": "test_reason",
            "original_space": "team:project",
            "final_space": "team:project",
        }


class TestCreateEngineFromSettings:
    """create_engine_from_settings 工厂函数测试"""

    def test_creates_engine_with_settings(self):
        """从 settings 创建引擎"""
        settings = {
            "team_write_enabled": True,
            "policy_json": {"max_chars": 500},
            "project_key": "myproject",
        }
        engine = create_engine_from_settings(settings)
        assert engine.team_write_enabled is True
        assert engine.policy["max_chars"] == 500
        assert engine.project_key == "myproject"

    def test_defaults_when_settings_empty(self):
        """settings 为空时使用默认值"""
        engine = create_engine_from_settings({})
        assert engine.team_write_enabled is False
        assert engine.policy == PolicyEngine.DEFAULT_POLICY


class TestDecideWrite:
    """decide_write 便捷函数测试"""

    def test_returns_correct_format(self):
        """返回正确的格式 {action, target_space, reason}"""
        result = decide_write(
            actor_user_id="alice",
            requested_space="private:alice",
            payload_md="content",
        )
        assert "action" in result
        assert "target_space" in result
        assert "reason" in result
        assert result["action"] == "allow"
        assert result["target_space"] == "private:alice"

    def test_with_policy_engine(self):
        """使用提供的 policy_engine"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"require_evidence": False, "allowed_kinds": []},
        )
        result = decide_write(
            actor_user_id="alice",
            requested_space="team:project",
            payload_md="short",
            policy_engine=engine,
        )
        assert result["action"] == "allow"
        assert result["target_space"] == "team:project"

    def test_with_settings(self):
        """使用 settings 创建引擎"""
        settings = {
            "team_write_enabled": True,
            "policy_json": {"require_evidence": False, "allowed_kinds": []},
        }
        result = decide_write(
            actor_user_id="alice",
            requested_space="team:project",
            payload_md="short",
            settings=settings,
        )
        assert result["action"] == "allow"

    def test_default_conservative_mode(self):
        """默认保守模式（禁止团队写入）"""
        result = decide_write(
            actor_user_id="alice",
            requested_space="team:project",
            payload_md="content",
        )
        assert result["action"] == "redirect"
        assert result["reason"] == "team_write_disabled"
        assert result["target_space"] == "private:alice"

    def test_payload_length_check(self):
        """payload 长度检查"""
        settings = {
            "team_write_enabled": True,
            "policy_json": {
                "max_chars": 50,
                "require_evidence": False,
                "allowed_kinds": [],
            },
        }
        result = decide_write(
            actor_user_id="alice",
            requested_space="team:project",
            payload_md="x" * 100,
            settings=settings,
        )
        assert result["action"] == "redirect"
        assert "exceeds_max_chars" in result["reason"]

    def test_evidence_missing_check(self):
        """证据缺失检查"""
        settings = {
            "team_write_enabled": True,
            "policy_json": {"require_evidence": True, "allowed_kinds": []},
        }
        result = decide_write(
            actor_user_id="alice",
            requested_space="team:project",
            payload_md="short",
            evidence_refs=None,
            settings=settings,
        )
        assert result["action"] == "redirect"
        assert result["reason"] == "missing_evidence"

    def test_kind_not_allowed_check(self):
        """kind 不允许检查"""
        settings = {
            "team_write_enabled": True,
            "policy_json": {
                "allowed_kinds": ["PROCEDURE"],
                "require_evidence": False,
            },
        }
        result = decide_write(
            actor_user_id="alice",
            requested_space="team:project",
            kind="FACT",
            payload_md="short",
            settings=settings,
        )
        assert result["action"] == "redirect"
        assert result["reason"] == "kind_not_allowed:FACT"

    def test_bulk_mode_check(self):
        """bulk 模式检查"""
        settings = {
            "team_write_enabled": True,
            "policy_json": {
                "bulk_mode": "reject",
                "require_evidence": False,
                "allowed_kinds": [],
            },
        }
        result = decide_write(
            actor_user_id="alice",
            requested_space="team:project",
            is_bulk=True,
            payload_md="short",
            settings=settings,
        )
        assert result["action"] == "redirect"
        assert result["reason"] == "bulk_not_allowed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
