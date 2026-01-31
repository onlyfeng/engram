"""
policy 模块单元测试

测试覆盖:
- PolicyEngine 各分支策略决策
- decide_write 便捷函数
- 典型场景: payload 长度、证据缺失、kind 不允许、bulk 模式
"""

import pytest

from engram.gateway.policy import (
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


class TestPolicyEngineEvidenceMode:
    """evidence_mode 相关测试"""

    def test_default_evidence_mode_is_compat(self):
        """默认 evidence_mode 为 compat"""
        engine = PolicyEngine(team_write_enabled=True)
        assert engine.evidence_mode == "compat"

    def test_evidence_mode_strict(self):
        """可以设置 strict 模式"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"evidence_mode": "strict"},
        )
        assert engine.evidence_mode == "strict"

    def test_invalid_evidence_mode_falls_back_to_compat(self):
        """无效的 evidence_mode 降级为 compat"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"evidence_mode": "invalid_mode"},
        )
        assert engine.evidence_mode == "compat"


class TestPolicyEngineBulkMaxChars:
    """bulk_max_chars 相关测试"""

    def test_default_bulk_max_chars(self):
        """默认 bulk_max_chars 为 200"""
        engine = PolicyEngine(team_write_enabled=True)
        assert engine.bulk_max_chars == 200

    def test_custom_bulk_max_chars(self):
        """可以自定义 bulk_max_chars"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"bulk_max_chars": 500},
        )
        assert engine.bulk_max_chars == 500

    def test_bulk_mode_very_short_uses_custom_limit(self):
        """bulk_mode=very_short 使用自定义字符限制"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "bulk_mode": "very_short",
                "bulk_max_chars": 100,  # 自定义为 100
                "require_evidence": False,
                "allowed_kinds": [],
            },
        )
        # 80 字符，在 100 限制内，应该通过
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="x" * 80,
            is_bulk=True,
        )
        assert decision.action == PolicyAction.ALLOW

        # 150 字符，超过 100 限制，应该重定向
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="x" * 150,
            is_bulk=True,
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "bulk_too_long"


class TestPolicyEngineProperties:
    """PolicyEngine 属性访问器测试"""

    def test_require_evidence_property(self):
        """require_evidence 属性测试"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"require_evidence": False},
        )
        assert engine.require_evidence is False

        engine2 = PolicyEngine(team_write_enabled=True)
        assert engine2.require_evidence is True


class TestTeamWriteDisabledScenarios:
    """team_write_disabled 场景覆盖"""

    def test_team_write_disabled_with_allowlist_user(self):
        """即使用户在白名单，team_write_disabled 时仍然重定向"""
        engine = PolicyEngine(
            team_write_enabled=False,  # 开关关闭
            policy_json={"allowlist_users": ["alice"]},  # alice 在白名单
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
        )
        # team_write_enabled=False 是第一道检查，应该先触发
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "team_write_disabled"

    def test_team_write_disabled_org_space(self):
        """org 空间在 team_write_disabled 时也被重定向"""
        engine = PolicyEngine(team_write_enabled=False)
        decision = engine.decide(
            target_space="org:shared",
            actor_user_id="alice",
            payload_md="content",
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "team_write_disabled"


class TestAllowlistScenarios:
    """allowlist 场景覆盖"""

    def test_empty_allowlist_allows_all_users(self):
        """空白名单允许所有用户"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "allowlist_users": [],  # 空列表
                "require_evidence": False,
                "allowed_kinds": [],
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="random_user",
            payload_md="content",
        )
        assert decision.action == PolicyAction.ALLOW

    def test_allowlist_case_sensitive(self):
        """白名单是大小写敏感的"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "allowlist_users": ["Alice"],  # 大写 A
                "require_evidence": False,
                "allowed_kinds": [],
            },
        )
        # 小写 alice 不在白名单
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "user_not_in_allowlist"

        # 大写 Alice 在白名单
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="Alice",
            payload_md="content",
        )
        assert decision.action == PolicyAction.ALLOW

    def test_allowlist_with_none_user(self):
        """actor_user_id 为 None 时的白名单检查"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"allowlist_users": ["alice"]},
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id=None,  # 无用户
            payload_md="content",
        )
        # None 不在白名单中
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "user_not_in_allowlist"


class TestMissingEvidenceScenarios:
    """missing_evidence 场景覆盖"""

    def test_require_evidence_false_allows_no_evidence(self):
        """require_evidence=False 时允许无证据"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "require_evidence": False,
                "allowed_kinds": [],
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
            evidence_refs=None,
        )
        assert decision.action == PolicyAction.ALLOW

    def test_evidence_with_empty_string_is_considered_present(self):
        """包含空字符串的证据列表被视为有证据"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "require_evidence": True,
                "allowed_kinds": [],
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
            evidence_refs=[""],  # 空字符串
        )
        # 列表非空，通过存在性检查
        assert decision.action == PolicyAction.ALLOW


class TestStrictEvidenceValidation:
    """strict evidence 校验场景"""

    def test_strict_mode_stored_in_policy(self):
        """strict 模式正确存储在 policy 中"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"evidence_mode": "strict"},
        )
        assert engine.policy["evidence_mode"] == "strict"

    def test_compat_mode_stored_in_policy(self):
        """compat 模式正确存储在 policy 中"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={"evidence_mode": "compat"},
        )
        assert engine.policy["evidence_mode"] == "compat"


class TestEvidencePresentParameter:
    """evidence_present 参数测试（v2 evidence 支持）"""

    def test_v2_evidence_present_true_bypasses_evidence_refs_check(self):
        """
        核心契约测试：v2 evidence + evidence_refs=None 时不应触发 missing_evidence

        场景：
        - evidence_refs=None（v1 legacy 格式为空）
        - evidence_present=True（经过 normalize_evidence 后有 v2 evidence）

        预期：策略通过，不触发 missing_evidence
        """
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "require_evidence": True,
                "allowed_kinds": [],
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
            evidence_refs=None,  # v1 格式为空
            evidence_present=True,  # v2 格式有 evidence
        )
        assert decision.action == PolicyAction.ALLOW
        assert decision.reason == "policy_passed"

    def test_v2_evidence_present_false_triggers_missing_evidence(self):
        """
        evidence_present=False 时应触发 missing_evidence
        """
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "require_evidence": True,
                "allowed_kinds": [],
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
            evidence_refs=None,
            evidence_present=False,  # 明确无 evidence
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "missing_evidence"

    def test_evidence_present_none_falls_back_to_evidence_refs(self):
        """
        evidence_present=None 时回退到 evidence_refs 检查（向后兼容）
        """
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "require_evidence": True,
                "allowed_kinds": [],
            },
        )
        # evidence_present=None, evidence_refs=None -> missing_evidence
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
            evidence_refs=None,
            evidence_present=None,
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "missing_evidence"

        # evidence_present=None, evidence_refs=["ref"] -> 通过
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
            evidence_refs=["some_ref"],
            evidence_present=None,
        )
        assert decision.action == PolicyAction.ALLOW

    def test_evidence_present_true_with_empty_evidence_refs_passes(self):
        """
        evidence_present=True + evidence_refs=[] 时应通过
        """
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "require_evidence": True,
                "allowed_kinds": [],
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
            evidence_refs=[],  # 空列表
            evidence_present=True,  # 有 v2 evidence
        )
        assert decision.action == PolicyAction.ALLOW
        assert decision.reason == "policy_passed"

    def test_evidence_present_overrides_evidence_refs(self):
        """
        evidence_present 优先于 evidence_refs
        """
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "require_evidence": True,
                "allowed_kinds": [],
            },
        )
        # evidence_refs 非空但 evidence_present=False -> missing_evidence
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
            evidence_refs=["ref1", "ref2"],  # v1 有 refs
            evidence_present=False,  # 但明确指定 v2 无 evidence
        )
        assert decision.action == PolicyAction.REDIRECT
        assert decision.reason == "missing_evidence"


class TestDecideWriteWithEvidencePresent:
    """decide_write 便捷函数的 evidence_present 参数测试"""

    def test_decide_write_with_evidence_present_true(self):
        """decide_write 支持 evidence_present 参数"""
        settings = {
            "team_write_enabled": True,
            "policy_json": {
                "require_evidence": True,
                "allowed_kinds": [],
            },
        }
        result = decide_write(
            actor_user_id="alice",
            requested_space="team:project",
            payload_md="content",
            evidence_refs=None,
            settings=settings,
            evidence_present=True,
        )
        assert result["action"] == "allow"
        assert result["reason"] == "policy_passed"

    def test_decide_write_with_evidence_present_false(self):
        """decide_write evidence_present=False 触发 missing_evidence"""
        settings = {
            "team_write_enabled": True,
            "policy_json": {
                "require_evidence": True,
                "allowed_kinds": [],
            },
        }
        result = decide_write(
            actor_user_id="alice",
            requested_space="team:project",
            payload_md="content",
            evidence_refs=None,
            settings=settings,
            evidence_present=False,
        )
        assert result["action"] == "redirect"
        assert result["reason"] == "missing_evidence"


class TestPolicyCheckOrder:
    """策略检查顺序测试"""

    def test_team_write_disabled_checked_first(self):
        """team_write_disabled 是第一道检查"""
        engine = PolicyEngine(
            team_write_enabled=False,
            policy_json={
                "allowlist_users": [],  # 不限制用户
                "allowed_kinds": [],  # 不限制类型
                "require_evidence": False,  # 不要求证据
                "max_chars": 10000,  # 高限制
            },
        )
        # 即使其他条件都满足，开关关闭时仍重定向
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="short",
        )
        assert decision.reason == "team_write_disabled"

    def test_allowlist_checked_before_kind(self):
        """白名单检查在类型检查之前"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "allowlist_users": ["bob"],  # alice 不在白名单
                "allowed_kinds": [],  # 不限制类型
                "require_evidence": False,
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
            kind="FACT",  # 即使有 kind
        )
        assert decision.reason == "user_not_in_allowlist"

    def test_kind_checked_before_evidence(self):
        """类型检查在证据检查之前"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "allowlist_users": [],  # 不限制用户
                "allowed_kinds": ["PROCEDURE"],  # 限制类型
                "require_evidence": True,  # 要求证据
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="content",
            kind="FACT",  # FACT 不在允许列表
            evidence_refs=None,  # 无证据
        )
        # kind 检查在 evidence 之前
        assert decision.reason == "kind_not_allowed:FACT"

    def test_evidence_checked_before_max_chars(self):
        """证据检查在字符限制检查之前"""
        engine = PolicyEngine(
            team_write_enabled=True,
            policy_json={
                "allowlist_users": [],
                "allowed_kinds": [],
                "require_evidence": True,  # 要求证据
                "max_chars": 10,  # 很低的限制
            },
        )
        decision = engine.decide(
            target_space="team:myproject",
            actor_user_id="alice",
            payload_md="this is very long content",  # 超过 10 字符
            evidence_refs=None,  # 无证据
        )
        # evidence 检查在 max_chars 之前
        assert decision.reason == "missing_evidence"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
