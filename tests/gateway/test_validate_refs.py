"""
渐进式校验开关测试

测试覆盖:
- validate_refs=True 时，无效 patches 触发 ValidationError
- validate_refs=False 时（默认），无效 patches 不触发错误
- 配置开关 VALIDATE_EVIDENCE_REFS 是否生效
- resolve_validate_refs 决策函数逻辑测试:
  - strict + enforce=true 必须为 True
  - strict + enforce=false 允许被环境变量关闭
  - compat 不强制
"""

import os
import pytest


# ===================== resolve_validate_refs 决策函数测试 =====================


class TestResolveValidateRefs:
    """测试 resolve_validate_refs 统一决策函数"""
    
    def test_strict_enforced_always_true(self):
        """strict 模式 + enforce=True: 必须为 True，忽略所有 override"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        # 构造 strict_mode_enforce_validate_refs=True 的配置
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=False,  # 环境变量设为 False
            strict_mode_enforce_validate_refs=True,  # 强制启用
        )
        
        # strict + enforce=True: 必须返回 True
        decision = resolve_validate_refs(mode="strict", config=config, caller_override=None)
        assert decision.effective is True
        assert decision.reason == "strict_enforced"
        
        # 即使 caller 尝试 override，也必须返回 True
        decision = resolve_validate_refs(mode="strict", config=config, caller_override=False)
        assert decision.effective is True
        assert decision.reason == "strict_enforced"
    
    def test_strict_enforce_false_allows_env_override(self):
        """strict 模式 + enforce=False: 允许被环境变量关闭"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        # 构造 strict_mode_enforce_validate_refs=False 的配置
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=False,  # 环境变量设为 False
            strict_mode_enforce_validate_refs=False,  # 允许 override
        )
        
        # strict + enforce=False + 环境变量 False: 应返回 False
        decision = resolve_validate_refs(mode="strict", config=config, caller_override=None)
        assert decision.effective is False
        assert decision.reason == "strict_env_override"
    
    def test_strict_enforce_false_allows_caller_override(self):
        """strict 模式 + enforce=False: 允许调用方显式 override"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=True,  # 环境变量设为 True
            strict_mode_enforce_validate_refs=False,  # 允许 override
        )
        
        # 调用方显式设为 False
        decision = resolve_validate_refs(mode="strict", config=config, caller_override=False)
        assert decision.effective is False
        assert decision.reason == "strict_caller_override"
        
        # 调用方显式设为 True
        decision = resolve_validate_refs(mode="strict", config=config, caller_override=True)
        assert decision.effective is True
        assert decision.reason == "strict_caller_override"
    
    def test_compat_uses_env_default(self):
        """compat 模式: 使用环境变量配置默认值"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        # validate_evidence_refs=False
        config_false = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=False,
            strict_mode_enforce_validate_refs=True,  # 这个在 compat 模式下无效
        )
        
        decision = resolve_validate_refs(mode="compat", config=config_false, caller_override=None)
        assert decision.effective is False
        assert decision.reason == "compat_default"
        
        # validate_evidence_refs=True
        config_true = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=True,
            strict_mode_enforce_validate_refs=True,
        )
        
        decision = resolve_validate_refs(mode="compat", config=config_true, caller_override=None)
        assert decision.effective is True
        assert decision.reason == "compat_default"
    
    def test_compat_allows_caller_override(self):
        """compat 模式: 允许调用方 override"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=True,  # 环境变量设为 True
            strict_mode_enforce_validate_refs=True,
        )
        
        # 调用方显式设为 False
        decision = resolve_validate_refs(mode="compat", config=config, caller_override=False)
        assert decision.effective is False
        assert decision.reason == "compat_caller_override"
        
        # 调用方显式设为 True
        decision = resolve_validate_refs(mode="compat", config=config, caller_override=True)
        assert decision.effective is True
        assert decision.reason == "compat_caller_override"
    
    def test_decision_to_dict(self):
        """ValidateRefsDecision.to_dict 返回正确结构"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=True,
            strict_mode_enforce_validate_refs=True,
        )
        
        decision = resolve_validate_refs(mode="strict", config=config)
        result = decision.to_dict()
        
        assert "validate_refs_effective" in result
        assert "validate_refs_reason" in result
        assert result["validate_refs_effective"] is True
        assert result["validate_refs_reason"] == "strict_enforced"
    
    def test_mode_case_insensitive(self):
        """mode 参数应不区分大小写"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=False,
            strict_mode_enforce_validate_refs=True,
        )
        
        # 大写 STRICT
        decision = resolve_validate_refs(mode="STRICT", config=config)
        assert decision.effective is True
        assert decision.reason == "strict_enforced"
        
        # 混合大小写 Compat
        decision = resolve_validate_refs(mode="Compat", config=config)
        assert decision.effective is False
        assert decision.reason == "compat_default"
    
    def test_none_mode_defaults_to_compat(self):
        """mode 为 None 时默认使用 compat"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=True,
            strict_mode_enforce_validate_refs=True,
        )
        
        decision = resolve_validate_refs(mode=None, config=config)
        assert decision.reason == "compat_default"


class TestValidateRefsConfig:
    """配置开关测试"""

    def test_config_validate_evidence_refs_default_false(self):
        """配置默认值应为 False（向后兼容）"""
        # 清理环境变量
        old_val = os.environ.pop("VALIDATE_EVIDENCE_REFS", None)
        
        # 重新加载配置
        from engram.gateway.config import reset_config, load_config
        reset_config()
        
        # 设置必需的环境变量
        os.environ.setdefault("PROJECT_KEY", "test_project")
        os.environ.setdefault("POSTGRES_DSN", "postgresql://localhost/test")
        os.environ.setdefault("OPENMEMORY_BASE_URL", "http://localhost:8080")
        
        try:
            config = load_config()
            assert config.validate_evidence_refs is False
        finally:
            reset_config()
            if old_val is not None:
                os.environ["VALIDATE_EVIDENCE_REFS"] = old_val

    def test_config_validate_evidence_refs_true(self):
        """设置 VALIDATE_EVIDENCE_REFS=true 时应为 True"""
        # 保存并设置环境变量
        old_val = os.environ.get("VALIDATE_EVIDENCE_REFS")
        os.environ["VALIDATE_EVIDENCE_REFS"] = "true"
        
        # 重新加载配置
        from engram.gateway.config import reset_config, load_config
        reset_config()
        
        # 设置必需的环境变量
        os.environ.setdefault("PROJECT_KEY", "test_project")
        os.environ.setdefault("POSTGRES_DSN", "postgresql://localhost/test")
        os.environ.setdefault("OPENMEMORY_BASE_URL", "http://localhost:8080")
        
        try:
            config = load_config()
            assert config.validate_evidence_refs is True
        finally:
            reset_config()
            if old_val is not None:
                os.environ["VALIDATE_EVIDENCE_REFS"] = old_val
            else:
                os.environ.pop("VALIDATE_EVIDENCE_REFS", None)


# ===================== validate_refs 决策参数化矩阵测试 =====================


class TestResolveValidateRefsMatrix:
    """
    resolve_validate_refs 参数化矩阵测试
    
    覆盖维度:
    - mode: strict/compat/None/大小写变体
    - validate_evidence_refs 环境配置: True/False
    - strict_mode_enforce_validate_refs: True/False
    - caller_override: True/False/None
    
    关键断言:
    - effective 最终有效值
    - reason 可解释性
    """
    
    # 参数化: (mode, env_validate, enforce, caller_override, expected_effective, expected_reason)
    VALIDATE_REFS_DECISION_CASES = [
        # ===== strict 模式 + enforce=True: 强制 True =====
        ("strict", False, True, None, True, "strict_enforced"),
        ("strict", False, True, False, True, "strict_enforced"),  # caller override 被忽略
        ("strict", False, True, True, True, "strict_enforced"),
        ("strict", True, True, None, True, "strict_enforced"),
        ("strict", True, True, False, True, "strict_enforced"),
        # 大小写变体
        ("STRICT", False, True, None, True, "strict_enforced"),
        ("Strict", False, True, None, True, "strict_enforced"),
        
        # ===== strict 模式 + enforce=False: 允许 override =====
        ("strict", False, False, None, False, "strict_env_override"),  # 使用环境变量
        ("strict", True, False, None, True, "strict_env_override"),
        ("strict", True, False, False, False, "strict_caller_override"),  # caller 显式关闭
        ("strict", False, False, True, True, "strict_caller_override"),  # caller 显式开启
        
        # ===== compat 模式: 不强制 =====
        ("compat", False, True, None, False, "compat_default"),  # enforce 在 compat 下无效
        ("compat", True, True, None, True, "compat_default"),
        ("compat", True, True, False, False, "compat_caller_override"),
        ("compat", False, True, True, True, "compat_caller_override"),
        # 大小写变体
        ("COMPAT", False, True, None, False, "compat_default"),
        ("Compat", True, False, None, True, "compat_default"),
        
        # ===== mode 为 None: 默认使用 compat =====
        (None, False, True, None, False, "compat_default"),
        (None, True, True, None, True, "compat_default"),
        (None, True, False, False, False, "compat_caller_override"),
    ]
    
    @pytest.mark.parametrize(
        "mode,env_validate,enforce,caller_override,expected_effective,expected_reason",
        VALIDATE_REFS_DECISION_CASES
    )
    def test_resolve_validate_refs_matrix(
        self, mode, env_validate, enforce, caller_override, expected_effective, expected_reason
    ):
        """参数化测试: resolve_validate_refs 决策矩阵"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=env_validate,
            strict_mode_enforce_validate_refs=enforce,
        )
        
        decision = resolve_validate_refs(mode=mode, config=config, caller_override=caller_override)
        
        assert decision.effective == expected_effective, \
            f"expected effective={expected_effective}, got {decision.effective}"
        assert decision.reason == expected_reason, \
            f"expected reason='{expected_reason}', got '{decision.reason}'"
    
    def test_decision_to_dict_structure(self):
        """ValidateRefsDecision.to_dict 返回正确结构"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=True,
            strict_mode_enforce_validate_refs=True,
        )
        
        decision = resolve_validate_refs(mode="strict", config=config)
        result = decision.to_dict()
        
        # 验证必需字段
        assert "validate_refs_effective" in result
        assert "validate_refs_reason" in result
        assert result["validate_refs_effective"] is True
        assert result["validate_refs_reason"] == "strict_enforced"
    
    @pytest.mark.parametrize("mode", ["strict", "compat", None])
    @pytest.mark.parametrize("enforce", [True, False])
    def test_resolve_validate_refs_uses_global_config_when_none(self, mode, enforce):
        """config 为 None 时使用全局配置"""
        from engram.gateway.config import resolve_validate_refs, reset_config
        
        # 设置环境变量
        os.environ.setdefault("PROJECT_KEY", "test_project")
        os.environ.setdefault("POSTGRES_DSN", "postgresql://localhost/test")
        os.environ.setdefault("OPENMEMORY_BASE_URL", "http://localhost:8080")
        os.environ["VALIDATE_EVIDENCE_REFS"] = "true"
        os.environ["STRICT_MODE_ENFORCE_VALIDATE_REFS"] = "true" if enforce else "false"
        
        try:
            reset_config()
            # config=None 时应该自动加载全局配置
            decision = resolve_validate_refs(mode=mode, config=None)
            
            # 验证返回有效的决策
            assert decision.effective in [True, False]
            assert decision.reason in [
                "strict_enforced", "strict_env_override", "strict_caller_override",
                "compat_default", "compat_caller_override"
            ]
        finally:
            reset_config()
            os.environ.pop("VALIDATE_EVIDENCE_REFS", None)
            os.environ.pop("STRICT_MODE_ENFORCE_VALIDATE_REFS", None)


class TestValidateRefsEnforceMatrix:
    """
    strict_mode_enforce_validate_refs 配置参数化测试
    
    验证 enforce 配置对 strict/compat 模式的影响
    """
    
    @pytest.mark.parametrize(
        "enforce,caller_override,expected_effective",
        [
            # enforce=True: 忽略所有 override
            (True, None, True),
            (True, False, True),
            (True, True, True),
            # enforce=False: 允许 caller override
            (False, None, False),  # 使用环境变量（False）
            (False, False, False),
            (False, True, True),
        ]
    )
    def test_enforce_controls_override_in_strict_mode(
        self, enforce, caller_override, expected_effective
    ):
        """strict 模式下 enforce 控制 override 行为"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=False,  # 环境变量 False
            strict_mode_enforce_validate_refs=enforce,
        )
        
        decision = resolve_validate_refs(mode="strict", config=config, caller_override=caller_override)
        
        assert decision.effective == expected_effective
    
    @pytest.mark.parametrize("enforce", [True, False])
    def test_enforce_has_no_effect_in_compat_mode(self, enforce):
        """compat 模式下 enforce 无效"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=False,
            strict_mode_enforce_validate_refs=enforce,  # 这个在 compat 下无效
        )
        
        decision = resolve_validate_refs(mode="compat", config=config)
        
        # compat 模式下始终使用环境变量配置
        assert decision.effective is False
        assert decision.reason == "compat_default"


class TestValidateRefsReasonExplanation:
    """
    validate_refs reason 可解释性测试
    
    验证 reason 值的含义清晰、可追溯
    """
    
    REASON_MEANINGS = {
        "strict_enforced": "strict 模式强制启用，忽略所有 override",
        "strict_env_override": "strict 模式，由环境变量控制",
        "strict_caller_override": "strict 模式，由调用方显式指定",
        "compat_default": "compat 模式使用配置默认值",
        "compat_caller_override": "compat 模式由调用方显式指定",
    }
    
    @pytest.mark.parametrize("reason", list(REASON_MEANINGS.keys()))
    def test_all_reasons_are_documented(self, reason):
        """所有 reason 值都有文档说明"""
        assert reason in self.REASON_MEANINGS
        assert len(self.REASON_MEANINGS[reason]) > 10  # 有意义的描述
    
    def test_strict_enforced_reason_traceable(self):
        """strict_enforced reason 可追溯到配置"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=False,
            strict_mode_enforce_validate_refs=True,  # 这是导致 strict_enforced 的原因
        )
        
        decision = resolve_validate_refs(mode="strict", config=config)
        
        # reason 表明是 strict 模式强制的
        assert decision.reason == "strict_enforced"
        # 可以追溯到配置项
        assert config.strict_mode_enforce_validate_refs is True
    
    def test_caller_override_reason_traceable(self):
        """caller_override reason 可追溯到调用参数"""
        from engram.gateway.config import resolve_validate_refs, GatewayConfig
        
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://localhost/test",
            openmemory_base_url="http://localhost:8080",
            validate_evidence_refs=True,  # 环境变量 True
            strict_mode_enforce_validate_refs=False,  # 允许 override
        )
        
        # 调用方显式关闭
        decision = resolve_validate_refs(mode="strict", config=config, caller_override=False)
        
        # reason 表明是调用方 override 的
        assert decision.reason == "strict_caller_override"
        # effective 反映 caller 的意图
        assert decision.effective is False


def _check_db_available():
    """检查数据库是否可用"""
    dsn = os.environ.get("TEST_PG_DSN") or os.environ.get("POSTGRES_DSN")
    if not dsn:
        return False, "需要 TEST_PG_DSN 或 POSTGRES_DSN 环境变量"
    
    try:
        import psycopg
        conn = psycopg.connect(dsn, connect_timeout=2)
        conn.close()
        return True, None
    except Exception as e:
        return False, f"数据库连接失败: {e}"


class TestValidateRefsValidationOnly:
    """纯校验测试（不需要数据库连接）"""

    def test_validate_refs_true_rejects_invalid_patches_no_db(self):
        """validate_refs=True 时，ValidationError 在数据库连接之前抛出"""
        from engram.logbook import governance
        from engram.logbook.errors import ValidationError
        
        # 无效的 patches 结构（缺少 artifact_uri 字段）
        invalid_evidence = {
            "patches": [
                {
                    "sha256": "abc123",
                    "source_id": "1:rev",
                    "source_type": "svn",
                    "kind": "patch",
                    # 缺少 artifact_uri
                }
            ]
        }
        
        # validate_refs=True 时应抛出 ValidationError（在数据库连接之前）
        with pytest.raises(ValidationError) as exc_info:
            governance.insert_write_audit(
                actor_user_id="test_user",
                target_space="team:test",
                action="allow",
                reason="test",
                evidence_refs_json=invalid_evidence,
                validate_refs=True,
            )
        
        # 验证错误信息包含有用的诊断信息
        assert "patches" in str(exc_info.value) or "artifact_uri" in str(exc_info.value)

    def test_validate_refs_true_rejects_invalid_patches_type(self):
        """validate_refs=True 时，patches 非 list 类型触发 ValidationError"""
        from engram.logbook import governance
        from engram.logbook.errors import ValidationError
        
        # patches 不是 list 类型
        invalid_evidence = {
            "patches": "not_a_list"
        }
        
        with pytest.raises(ValidationError):
            governance.insert_write_audit(
                actor_user_id="test_user",
                target_space="team:test",
                action="allow",
                reason="test",
                evidence_refs_json=invalid_evidence,
                validate_refs=True,
            )

    def test_validate_refs_true_rejects_invalid_attachments_no_db(self):
        """validate_refs=True 时，无效 attachments 在数据库连接之前抛出"""
        from engram.logbook import governance
        from engram.logbook.errors import ValidationError
        
        # 无效的 attachments 结构（缺少 artifact_uri 字段）
        invalid_evidence = {
            "attachments": [
                {
                    "sha256": "a" * 64,
                    # 缺少 artifact_uri
                }
            ]
        }
        
        with pytest.raises(ValidationError) as exc_info:
            governance.insert_write_audit(
                actor_user_id="test_user",
                target_space="team:test",
                action="allow",
                reason="test",
                evidence_refs_json=invalid_evidence,
                validate_refs=True,
            )
        
        assert "attachments" in str(exc_info.value) or "artifact_uri" in str(exc_info.value)

    def test_validate_refs_true_rejects_invalid_external_no_db(self):
        """validate_refs=True 时，无效 external 在数据库连接之前抛出"""
        from engram.logbook import governance
        from engram.logbook.errors import ValidationError
        
        # 无效的 external 结构（缺少 uri 字段）
        invalid_evidence = {
            "external": [
                {
                    "description": "缺少 uri",
                }
            ]
        }
        
        with pytest.raises(ValidationError) as exc_info:
            governance.insert_write_audit(
                actor_user_id="test_user",
                target_space="team:test",
                action="allow",
                reason="test",
                evidence_refs_json=invalid_evidence,
                validate_refs=True,
            )
        
        assert "external" in str(exc_info.value) or "uri" in str(exc_info.value)


class TestValidateRefsGovernance:
    """governance 模块 validate_refs 参数测试（需要数据库）"""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """设置测试环境"""
        available, reason = _check_db_available()
        if not available:
            pytest.skip(reason)

    def test_validate_refs_false_accepts_invalid_patches(self):
        """validate_refs=False 时，无效 patches 不触发错误"""
        from engram.logbook import governance
        from engram.logbook.errors import ValidationError
        
        # 无效的 patches 结构（缺少 artifact_uri 字段）
        invalid_evidence = {
            "patches": [
                {
                    "sha256": "abc123",
                    "source_id": "1:rev",
                    "source_type": "svn",
                    "kind": "patch",
                    # 缺少 artifact_uri
                }
            ]
        }
        
        # validate_refs=False（默认）时不应抛出异常
        try:
            audit_id = governance.insert_write_audit(
                actor_user_id="test_user",
                target_space="team:test",
                action="allow",
                reason="test",
                evidence_refs_json=invalid_evidence,
                validate_refs=False,
            )
            assert audit_id > 0
        except ValidationError:
            pytest.fail("validate_refs=False 时不应抛出 ValidationError")

    def test_validate_refs_true_rejects_invalid_patches(self):
        """validate_refs=True 时，无效 patches 触发 ValidationError"""
        from engram.logbook import governance
        from engram.logbook.errors import ValidationError
        
        # 无效的 patches 结构（缺少 artifact_uri 字段）
        invalid_evidence = {
            "patches": [
                {
                    "sha256": "abc123",
                    "source_id": "1:rev",
                    "source_type": "svn",
                    "kind": "patch",
                    # 缺少 artifact_uri
                }
            ]
        }
        
        # validate_refs=True 时应抛出 ValidationError
        with pytest.raises(ValidationError) as exc_info:
            governance.insert_write_audit(
                actor_user_id="test_user",
                target_space="team:test",
                action="allow",
                reason="test",
                evidence_refs_json=invalid_evidence,
                validate_refs=True,
            )
        
        # 验证错误信息包含有用的诊断信息
        assert "patches" in str(exc_info.value) or "artifact_uri" in str(exc_info.value)

    def test_validate_refs_true_accepts_valid_patches(self):
        """validate_refs=True 时，有效 patches 正常写入"""
        from engram.logbook import governance
        from engram.logbook.uri import build_evidence_ref_for_patch_blob
        
        # 使用标准构建函数创建有效的 patch 引用
        valid_ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123def",
            content_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        valid_evidence = {
            "patches": [valid_ref]
        }
        
        # validate_refs=True 时有效数据应正常写入
        audit_id = governance.insert_write_audit(
            actor_user_id="test_user",
            target_space="team:test",
            action="allow",
            reason="test_valid_patches",
            evidence_refs_json=valid_evidence,
            validate_refs=True,
        )
        assert audit_id > 0


class TestValidateRefsAdapter:
    """logbook_adapter 模块 validate_refs 参数测试"""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """设置测试环境"""
        available, reason = _check_db_available()
        if not available:
            pytest.skip(reason)

    def test_adapter_passes_validate_refs_to_governance(self):
        """logbook_adapter.insert_audit 应将 validate_refs 透传到 governance"""
        from engram.gateway.logbook_adapter import LogbookAdapter
        from engram.logbook.errors import ValidationError
        
        adapter = LogbookAdapter()
        
        # 无效的 patches 结构
        invalid_evidence = {
            "patches": [
                {
                    "sha256": "abc123",
                    # 缺少必需字段
                }
            ]
        }
        
        # validate_refs=True 时应抛出 ValidationError
        with pytest.raises(ValidationError):
            adapter.insert_audit(
                actor_user_id="test_user",
                target_space="team:test",
                action="allow",
                reason="test",
                evidence_refs_json=invalid_evidence,
                validate_refs=True,
            )


class TestValidateRefsIntegration:
    """集成测试：验证配置驱动的校验行为"""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """设置测试环境"""
        available, reason = _check_db_available()
        if not available:
            pytest.skip(reason)

    def test_logbook_db_insert_audit_with_validate_refs(self):
        """LogbookDatabase.insert_audit 支持 validate_refs 参数"""
        from engram.gateway.logbook_db import LogbookDatabase
        from engram.logbook.errors import ValidationError
        
        dsn = os.environ.get("TEST_PG_DSN") or os.environ.get("POSTGRES_DSN")
        db = LogbookDatabase(dsn=dsn)
        
        # 无效的 patches 结构
        invalid_evidence = {
            "patches": [
                {"invalid": "structure"}
            ]
        }
        
        # validate_refs=True 时应抛出 ValidationError
        with pytest.raises(ValidationError):
            db.insert_audit(
                actor_user_id="test_user",
                target_space="team:test",
                action="allow",
                reason="test",
                evidence_refs_json=invalid_evidence,
                validate_refs=True,
            )
        
        # validate_refs=False（默认）时不应抛出
        audit_id = db.insert_audit(
            actor_user_id="test_user",
            target_space="team:test",
            action="allow",
            reason="test",
            evidence_refs_json=invalid_evidence,
            validate_refs=False,
        )
        assert audit_id > 0


# ===================== Gateway <-> Logbook URI 契约测试 =====================


# 测试用的有效 SHA256
VALID_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class TestGatewayLogbookUriContractAttachment:
    """
    契约测试：Gateway 写入的 attachment evidence URI 可被 Logbook 正确解析
    
    对应 03_memory_contract.md 中的要求:
    - attachment URI 格式必须严格遵循 Logbook parse_attachment_evidence_uri() 的解析规则
    - Gateway → Logbook：通过 parse_attachment_evidence_uri(uri) 解析出 attachment_id
    - Logbook → Gateway：通过 build_attachment_evidence_uri(attachment_id, sha256) 构建规范 URI
    """

    def test_gateway_attachment_uri_parsable_by_logbook(self):
        """Gateway 构建的 attachment URI 必须能被 Logbook 解析"""
        from engram.logbook.uri import (
            build_attachment_evidence_uri,
            parse_attachment_evidence_uri_strict,
        )
        
        # 模拟 Gateway 构建 attachment URI
        attachment_id = 12345
        sha256 = VALID_SHA256
        
        # 使用 Logbook 的 build 函数构建 URI（Gateway 应使用相同的函数）
        uri = build_attachment_evidence_uri(attachment_id, sha256)
        
        # Logbook 解析验证
        result = parse_attachment_evidence_uri_strict(uri)
        
        assert result.success is True, f"Logbook 应能解析: {result.error_message}"
        assert result.attachment_id == attachment_id
        assert result.sha256 == sha256

    def test_gateway_rejects_legacy_attachment_uri_format(self):
        """Gateway 不应使用旧格式的 attachment URI"""
        from engram.logbook.uri import (
            parse_attachment_evidence_uri_strict,
            ATTACHMENT_URI_ERR_LEGACY_FORMAT,
        )
        
        # 旧格式（三段路径）: memory://attachments/<namespace>/<id>/<sha256>
        legacy_uri = f"memory://attachments/my_namespace/123/{VALID_SHA256}"
        
        # Logbook 应拒绝解析
        result = parse_attachment_evidence_uri_strict(legacy_uri)
        
        assert result.success is False
        assert result.error_code == ATTACHMENT_URI_ERR_LEGACY_FORMAT
        assert "旧格式" in result.error_message

    def test_evidence_refs_json_attachment_contract(self):
        """Gateway evidence_refs_json 中的 attachment 引用必须符合规范
        
        规范结构:
        {
            "attachments": [{
                "artifact_uri": "memory://attachments/<attachment_id>/<sha256>",
                "sha256": "<sha256>",
                "attachment_id": <int>,
                "kind": "<kind>"
            }]
        }
        """
        from engram.logbook.uri import (
            build_attachment_evidence_ref,
            parse_attachment_evidence_uri_strict,
        )
        
        # 使用 Logbook 的标准构建函数
        ref = build_attachment_evidence_ref(
            attachment_id=12345,
            sha256=VALID_SHA256,
            kind="screenshot",
            item_id=100,
        )
        
        # 验证结构完整性
        assert "artifact_uri" in ref
        assert "sha256" in ref
        assert "attachment_id" in ref
        assert "kind" in ref
        
        # 验证 artifact_uri 可被解析
        result = parse_attachment_evidence_uri_strict(ref["artifact_uri"])
        assert result.success is True
        assert result.attachment_id == 12345
        assert result.sha256 == VALID_SHA256


class TestGatewayLogbookUriContractPatchBlob:
    """
    契约测试：Gateway 写入的 patch_blob evidence URI 可被 Logbook 正确解析
    
    对应 03_memory_contract.md 中的 Evidence 规范:
    - uri 可解析性要求：必须为可回溯的有效 URI
    - scheme 限 memory://、svn://、git://、https://
    """

    def test_gateway_patch_blob_uri_parsable_by_logbook(self):
        """Gateway 构建的 patch_blob URI 必须能被 Logbook 解析"""
        from engram.logbook.uri import (
            build_evidence_uri,
            parse_evidence_uri,
        )
        
        # 模拟 Gateway 构建 patch_blob URI
        source_type = "git"
        source_id = "1:abc123def"
        sha256 = VALID_SHA256
        
        uri = build_evidence_uri(source_type, source_id, sha256)
        
        # Logbook 解析验证
        result = parse_evidence_uri(uri)
        
        assert result is not None, "Logbook 应能解析 patch_blob URI"
        assert result["source_type"] == source_type
        assert result["source_id"] == source_id
        assert result["sha256"] == sha256

    def test_evidence_refs_json_patches_contract(self):
        """Gateway evidence_refs_json 中的 patches 引用必须符合规范
        
        规范结构:
        {
            "patches": [{
                "artifact_uri": "memory://patch_blobs/<source_type>/<source_id>/<sha256>",
                "sha256": "<sha256>",
                "source_id": "<source_id>",
                "source_type": "<svn|git>",
                "kind": "patch"
            }]
        }
        """
        from engram.logbook.uri import (
            build_evidence_ref_for_patch_blob,
            parse_evidence_uri,
            validate_evidence_ref,
        )
        
        # 使用 Logbook 的标准构建函数
        ref = build_evidence_ref_for_patch_blob(
            source_type="git",
            source_id="1:abc123def",
            sha256=VALID_SHA256,
            kind="patch",
        )
        
        # 验证必需字段
        assert "artifact_uri" in ref
        assert "sha256" in ref
        assert "source_id" in ref
        assert "source_type" in ref
        
        # 验证 artifact_uri 可被解析
        result = parse_evidence_uri(ref["artifact_uri"])
        assert result is not None
        
        # 验证整体结构有效
        is_valid, error = validate_evidence_ref(ref)
        assert is_valid, f"evidence ref 结构无效: {error}"


class TestGatewayMemoryCardUriContract:
    """
    契约测试：Gateway MemoryCard 中的 Evidence.uri 必须符合规范
    
    对应 memory_card.py 中 Evidence 类的要求:
    - uri 可解析性要求：必须为可回溯的有效 URI
    - scheme 限 memory://, svn://, git://, https://
    """

    def test_memory_card_evidence_uri_must_be_valid_scheme(self):
        """MemoryCard Evidence.uri 必须使用允许的 scheme"""
        from engram.gateway.memory_card import Evidence
        
        # 允许的 scheme
        allowed_uris = [
            f"memory://attachments/123/{VALID_SHA256}",
            f"memory://patch_blobs/git/1:abc/{VALID_SHA256}",
            "svn://repo/trunk@123",
            "git://repo.git/commit/abc123",
            f"https://github.com/repo/commit/{VALID_SHA256[:40]}",
        ]
        
        for uri in allowed_uris:
            ev = Evidence(uri=uri, sha256=VALID_SHA256)
            errors = ev.validate()
            assert not any("scheme" in err.lower() for err in errors), \
                f"URI {uri} 应该被允许，但验证失败: {errors}"

    def test_memory_card_evidence_uri_rejects_invalid_scheme(self):
        """MemoryCard Evidence.uri 必须拒绝未允许的 scheme"""
        from engram.gateway.memory_card import Evidence
        
        # 不允许的 scheme
        invalid_uris = [
            "ftp://server/file",
            "file:///local/path",
            "s3://bucket/key",
            "invalid://scheme",
        ]
        
        for uri in invalid_uris:
            ev = Evidence(uri=uri, sha256=VALID_SHA256)
            errors = ev.validate()
            assert any("scheme" in err.lower() for err in errors), \
                f"URI {uri} 应该被拒绝，但验证通过"

    def test_memory_card_evidence_sha256_must_be_valid(self):
        """MemoryCard Evidence.sha256 必须为 64 位十六进制"""
        from engram.gateway.memory_card import Evidence
        
        # 有效的 sha256
        ev_valid = Evidence(
            uri=f"memory://attachments/123/{VALID_SHA256}",
            sha256=VALID_SHA256,
        )
        assert ev_valid.validate() == []
        
        # 无效的 sha256
        ev_invalid = Evidence(
            uri=f"memory://attachments/123/{VALID_SHA256}",
            sha256="invalid_sha",
        )
        errors = ev_invalid.validate()
        assert any("sha256" in err.lower() for err in errors)

    def test_gateway_evidence_refs_roundtrip_logbook(self):
        """
        端到端契约测试：Gateway 写入的 evidence_refs_json 可被 Logbook 完整解析
        
        模拟场景：
        1. Gateway 使用 memory_card 生成 Evidence
        2. Evidence URI 写入 evidence_refs_json
        3. Logbook 解析 URI 并回查原始附件
        """
        from engram.gateway.memory_card import Evidence
        from engram.logbook.uri import (
            parse_attachment_evidence_uri_strict,
            parse_evidence_uri,
        )
        
        # 模拟 Gateway 构建的 Evidence（attachment 类型）
        attachment_uri = f"memory://attachments/12345/{VALID_SHA256}"
        ev_attachment = Evidence(uri=attachment_uri, sha256=VALID_SHA256)
        assert ev_attachment.validate() == []
        
        # Logbook 解析验证
        result = parse_attachment_evidence_uri_strict(attachment_uri)
        assert result.success is True, f"Logbook 无法解析 Gateway attachment URI: {result.error_message}"
        
        # 模拟 Gateway 构建的 Evidence（patch_blob 类型）
        patch_uri = f"memory://patch_blobs/git/1:abc123/{VALID_SHA256}"
        ev_patch = Evidence(uri=patch_uri, sha256=VALID_SHA256)
        assert ev_patch.validate() == []
        
        # Logbook 解析验证
        result_patch = parse_evidence_uri(patch_uri)
        assert result_patch is not None, "Logbook 无法解析 Gateway patch_blob URI"


# ===================== Evidence V2 规范化与校验测试 =====================


class TestEvidenceNormalization:
    """测试 evidence v2 规范化逻辑"""
    
    def test_v2_evidence_takes_priority(self):
        """v2 evidence 优先于 v1 evidence_refs"""
        from engram.gateway.audit_event import normalize_evidence
        
        v2_evidence = [
            {"uri": "memory://attachments/123/abc123def456", "sha256": "abc123def456" + "0" * 52}
        ]
        v1_refs = ["https://example.com/legacy.md"]
        
        result, source = normalize_evidence(v2_evidence, v1_refs)
        
        assert source == "v2"
        assert len(result) == 1
        assert result[0]["uri"] == "memory://attachments/123/abc123def456"
    
    def test_v1_refs_mapped_to_external(self):
        """v1 evidence_refs 映射为 v2 external 格式"""
        from engram.gateway.audit_event import normalize_evidence
        
        v1_refs = [
            "https://example.com/doc.md",
            "git://repo/commit/abc123",
        ]
        
        result, source = normalize_evidence(None, v1_refs)
        
        assert source == "v1_mapped"
        assert len(result) == 2
        
        # 验证映射结构
        assert result[0]["uri"] == "https://example.com/doc.md"
        assert result[0]["sha256"] == ""  # legacy refs 无 sha256
        assert result[0]["_source"] == "evidence_refs_legacy"
        
        assert result[1]["uri"] == "git://repo/commit/abc123"
        assert result[1]["sha256"] == ""
    
    def test_empty_inputs_return_none_source(self):
        """空输入返回 none source"""
        from engram.gateway.audit_event import normalize_evidence
        
        result, source = normalize_evidence(None, None)
        
        assert source == "none"
        assert result == []
    
    def test_empty_v2_uses_v1(self):
        """空 v2 列表回退到 v1"""
        from engram.gateway.audit_event import normalize_evidence
        
        v1_refs = ["https://example.com/doc.md"]
        
        result, source = normalize_evidence([], v1_refs)
        
        assert source == "v1_mapped"
        assert len(result) == 1


class TestEvidenceValidationStrictMode:
    """测试 strict 模式下的 evidence 校验"""
    
    def test_valid_evidence_passes(self):
        """有效的 v2 evidence 校验通过"""
        from engram.gateway.audit_event import validate_evidence_for_strict_mode
        
        valid_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        evidence = [
            {"uri": "memory://attachments/123/" + valid_sha, "sha256": valid_sha}
        ]
        
        result = validate_evidence_for_strict_mode(evidence)
        
        assert result.is_valid is True
        assert result.error_codes == []
    
    def test_missing_uri_fails(self):
        """缺少 uri 字段校验失败"""
        from engram.gateway.audit_event import validate_evidence_for_strict_mode
        
        evidence = [
            {"sha256": "abc123" + "0" * 58}  # 缺少 uri
        ]
        
        result = validate_evidence_for_strict_mode(evidence)
        
        assert result.is_valid is False
        assert any("EVIDENCE_MISSING_URI" in code for code in result.error_codes)
    
    def test_missing_sha256_fails_for_v2(self):
        """v2 evidence 缺少 sha256 校验失败"""
        from engram.gateway.audit_event import validate_evidence_for_strict_mode
        
        evidence = [
            {"uri": "memory://attachments/123/abc"}  # 缺少 sha256
        ]
        
        result = validate_evidence_for_strict_mode(evidence)
        
        assert result.is_valid is False
        assert any("EVIDENCE_MISSING_SHA256" in code for code in result.error_codes)
    
    def test_legacy_source_missing_sha256_is_warning(self):
        """legacy 来源缺少 sha256 仅产生警告"""
        from engram.gateway.audit_event import validate_evidence_for_strict_mode
        
        evidence = [
            {
                "uri": "https://example.com/doc.md",
                "sha256": "",
                "_source": "evidence_refs_legacy",
            }
        ]
        
        result = validate_evidence_for_strict_mode(evidence)
        
        # legacy 来源缺少 sha256 不算错误
        assert result.is_valid is True
        assert result.error_codes == []
        # 但产生警告
        assert any("EVIDENCE_LEGACY_NO_SHA256" in warn for warn in result.compat_warnings)
    
    def test_invalid_sha256_format_fails(self):
        """无效的 sha256 格式校验失败"""
        from engram.gateway.audit_event import validate_evidence_for_strict_mode
        
        evidence = [
            {"uri": "memory://attachments/123/abc", "sha256": "not_valid_sha256"}
        ]
        
        result = validate_evidence_for_strict_mode(evidence)
        
        assert result.is_valid is False
        assert any("EVIDENCE_INVALID_SHA256" in code for code in result.error_codes)
    
    def test_validation_result_to_dict(self):
        """EvidenceValidationResult.to_dict 返回正确结构"""
        from engram.gateway.audit_event import validate_evidence_for_strict_mode
        
        evidence = [
            {"uri": "memory://attachments/123/abc"}  # 缺少 sha256
        ]
        
        result = validate_evidence_for_strict_mode(evidence)
        result_dict = result.to_dict()
        
        assert "is_valid" in result_dict
        assert "error_codes" in result_dict
        assert "compat_warnings" in result_dict


class TestMapEvidenceRefsToV2External:
    """测试 evidence_refs → v2 external 映射"""
    
    def test_basic_mapping(self):
        """基本映射测试"""
        from engram.gateway.audit_event import map_evidence_refs_to_v2_external
        
        refs = [
            "https://example.com/doc.md",
            "git://repo/commit/abc123",
            "svn://repo/trunk@100",
        ]
        
        result = map_evidence_refs_to_v2_external(refs)
        
        assert len(result) == 3
        for item in result:
            assert "uri" in item
            assert item["sha256"] == ""
            assert item["_source"] == "evidence_refs_legacy"
    
    def test_empty_refs_return_empty_list(self):
        """空 refs 返回空列表"""
        from engram.gateway.audit_event import map_evidence_refs_to_v2_external
        
        assert map_evidence_refs_to_v2_external(None) == []
        assert map_evidence_refs_to_v2_external([]) == []
    
    def test_skips_empty_strings(self):
        """跳过空字符串"""
        from engram.gateway.audit_event import map_evidence_refs_to_v2_external
        
        refs = ["https://example.com/doc.md", "", "git://repo/abc"]
        
        result = map_evidence_refs_to_v2_external(refs)
        
        assert len(result) == 2
        assert result[0]["uri"] == "https://example.com/doc.md"
        assert result[1]["uri"] == "git://repo/abc"


class TestGatewayLogbookUriErrorCodeContract:
    """
    契约测试：Gateway 必须正确处理 Logbook 返回的 URI 解析错误码
    
    验证 Gateway 代码能识别并处理所有定义的错误码
    """

    def test_all_error_codes_are_defined(self):
        """验证所有 attachment URI 错误码都已定义"""
        from engram.logbook.uri import (
            ATTACHMENT_URI_ERR_NOT_MEMORY,
            ATTACHMENT_URI_ERR_NOT_ATTACHMENTS,
            ATTACHMENT_URI_ERR_LEGACY_FORMAT,
            ATTACHMENT_URI_ERR_INVALID_ID,
            ATTACHMENT_URI_ERR_INVALID_SHA256,
            ATTACHMENT_URI_ERR_MALFORMED,
        )
        
        # 验证错误码是字符串且非空
        error_codes = [
            ATTACHMENT_URI_ERR_NOT_MEMORY,
            ATTACHMENT_URI_ERR_NOT_ATTACHMENTS,
            ATTACHMENT_URI_ERR_LEGACY_FORMAT,
            ATTACHMENT_URI_ERR_INVALID_ID,
            ATTACHMENT_URI_ERR_INVALID_SHA256,
            ATTACHMENT_URI_ERR_MALFORMED,
        ]
        
        for code in error_codes:
            assert isinstance(code, str)
            assert len(code) > 0
            assert code.startswith("E_")

    def test_legacy_format_error_is_distinct(self):
        """旧格式错误码必须与其他错误码不同，便于 Gateway 特殊处理"""
        from engram.logbook.uri import (
            ATTACHMENT_URI_ERR_LEGACY_FORMAT,
            ATTACHMENT_URI_ERR_INVALID_ID,
            parse_attachment_evidence_uri_strict,
        )
        
        # 旧格式
        result_legacy = parse_attachment_evidence_uri_strict(
            f"memory://attachments/namespace/123/{VALID_SHA256}"
        )
        
        # 非整数 ID
        result_invalid = parse_attachment_evidence_uri_strict(
            f"memory://attachments/abc/{VALID_SHA256}"
        )
        
        # 错误码必须不同
        assert result_legacy.error_code != result_invalid.error_code
        assert result_legacy.error_code == ATTACHMENT_URI_ERR_LEGACY_FORMAT
        assert result_invalid.error_code == ATTACHMENT_URI_ERR_INVALID_ID
