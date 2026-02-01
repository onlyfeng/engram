"""
Profile 语义固定规则回归测试

本测试验证 docs/acceptance/00_acceptance_matrix.md 中定义的 Profile 语义固定规则，
确保关键字符串和函数在代码重构时不被意外修改。

回归锚点：
1. HTTP_ONLY_MODE 跳过原因字符串固定
2. FULL profile pytest.fail 前缀固定
3. Makefile 关键目标和变量存在
"""

from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent


class TestProfileSemanticsAnchors:
    """Profile 语义固定规则回归测试"""

    def test_http_only_skip_reason_fixed_string(self):
        """规则 1: HTTP_ONLY_MODE 跳过原因字符串必须保持固定"""
        # 读取 outbox worker 集成测试文件
        test_file = PROJECT_ROOT / "tests/gateway/test_outbox_worker_integration.py"
        content = test_file.read_text()

        # 验证固定字符串存在
        expected_reason = "HTTP_ONLY_MODE: Outbox Worker 集成测试需要 Docker 和数据库"
        assert expected_reason in content, (
            f"HTTP_ONLY_SKIP_REASON 字符串不存在或被修改。\n"
            f"预期: {expected_reason!r}\n"
            f"位置: tests/gateway/test_outbox_worker_integration.py"
        )

        # 验证 pytestmark 使用此 reason
        assert "pytestmark = pytest.mark.skipif" in content, "缺少模块级 pytestmark 定义"

    def test_http_only_skip_reason_prefix_anchor(self):
        """规则 1b: 所有 HTTP_ONLY_MODE 跳过原因必须使用 'HTTP_ONLY_MODE:' 前缀

        此规则确保 grep 锚点统一，便于搜索和维护。
        grep 锚点: "HTTP_ONLY_MODE:" 或 HTTP_ONLY_SKIP_REASON_PREFIX 常量可搜索到所有跳过原因。
        """
        # 需要验证的文件及其预期跳过原因（字面量部分，不含前缀）
        test_files_with_skip_reason = [
            (
                "tests/gateway/test_outbox_worker_integration.py",
                "Outbox Worker 集成测试需要 Docker 和数据库",
            ),
            (
                "tests/gateway/test_two_phase_audit_e2e.py",
                "两阶段审计测试需要 Docker 和数据库",
            ),
            (
                "tests/gateway/test_schema_prefix_search_path.py",
                "Schema 前缀测试需要 Docker 和数据库",
            ),
            (
                "tests/gateway/test_unified_stack_integration.py",
                "统一栈测试需要 Docker 和数据库",
            ),
        ]

        for file_path, expected_suffix in test_files_with_skip_reason:
            test_file = PROJECT_ROOT / file_path
            content = test_file.read_text()

            # 验证跳过原因字符串存在（可能是常量定义或字符串字面量）
            assert expected_suffix in content, (
                f"HTTP_ONLY_MODE 跳过原因后缀不匹配。\n文件: {file_path}\n预期: {expected_suffix!r}"
            )

            # 验证使用 'HTTP_ONLY_MODE:' 前缀格式（字面量或常量引用）
            uses_literal_prefix = "HTTP_ONLY_MODE:" in content
            uses_constant = "HTTP_ONLY_SKIP_REASON_PREFIX" in content
            assert uses_literal_prefix or uses_constant, (
                f"跳过原因应使用 'HTTP_ONLY_MODE:' 前缀或引用 HTTP_ONLY_SKIP_REASON_PREFIX 常量。\n"
                f"文件: {file_path}"
            )

    def test_full_profile_fail_prefix_fixed(self):
        """规则 2: FULL profile pytest.fail 前缀必须保持固定"""
        # 读取 unified stack 集成测试文件
        test_file = PROJECT_ROOT / "tests/gateway/test_unified_stack_integration.py"
        content = test_file.read_text()

        # 验证固定前缀存在（可能通过常量引用）
        expected_prefix = "[FULL profile]"
        # 前缀可能以字面量形式存在，或通过常量引用
        assert expected_prefix in content or "FULL_FAIL_PREFIX" in content, (
            f"FULL profile fail 前缀不存在或被修改。\n"
            f"预期: {expected_prefix!r} 或 FULL_FAIL_PREFIX 常量引用\n"
            f"位置: tests/gateway/test_unified_stack_integration.py"
        )

        # 验证 enforce_capability_or_fail 函数存在
        assert "def enforce_capability_or_fail" in content, (
            "缺少 enforce_capability_or_fail 函数定义"
        )

        # 验证 should_fail_if_blocked 函数存在
        assert "def should_fail_if_blocked" in content, "缺少 should_fail_if_blocked 函数定义"

    def test_gate_helpers_constants_defined(self):
        """规则 2b: gate_helpers.py 必须定义 Profile Skip/Fail 消息常量"""
        gate_helpers_file = PROJECT_ROOT / "tests/gateway/gate_helpers.py"
        content = gate_helpers_file.read_text()

        # 验证 HTTP_ONLY_SKIP_REASON_PREFIX 常量存在
        assert "HTTP_ONLY_SKIP_REASON_PREFIX" in content, (
            "gate_helpers.py 缺少 HTTP_ONLY_SKIP_REASON_PREFIX 常量定义"
        )
        # 验证前缀值符合 grep 锚点约定
        assert 'HTTP_ONLY_SKIP_REASON_PREFIX = "HTTP_ONLY_MODE: "' in content, (
            "HTTP_ONLY_SKIP_REASON_PREFIX 值必须为 'HTTP_ONLY_MODE: '"
        )

        # 验证 FULL_FAIL_PREFIX 常量存在
        assert "FULL_FAIL_PREFIX" in content, "gate_helpers.py 缺少 FULL_FAIL_PREFIX 常量定义"
        # 验证前缀值固定
        assert 'FULL_FAIL_PREFIX = "[FULL profile]"' in content, (
            "FULL_FAIL_PREFIX 值必须为 '[FULL profile]'"
        )

    def test_makefile_verify_unified_target_exists(self):
        """规则 3: Makefile verify-unified 目标必须存在"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 验证 verify-unified 目标存在
        assert "verify-unified:" in content, "Makefile 缺少 verify-unified 目标定义"

    def test_makefile_verify_full_variable_used(self):
        """规则 3: Makefile 必须使用 VERIFY_FULL 变量"""
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 验证 VERIFY_FULL 变量在 verify-unified 中使用
        assert "VERIFY_FULL" in content, "Makefile 缺少 VERIFY_FULL 变量使用"

    def test_must_fail_steps_documented_in_code(self):
        """验证 must_fail_if_blocked 步骤在代码中有对应处理"""
        test_file = PROJECT_ROOT / "tests/gateway/test_unified_stack_integration.py"
        content = test_file.read_text()

        # 验证关键步骤名称存在
        must_fail_steps = ["degradation", "db_invariants"]
        for step in must_fail_steps:
            # 步骤名称应该在 should_fail_if_blocked 的回退逻辑中
            assert step in content, f"must_fail_if_blocked 步骤 '{step}' 未在代码中定义"

    def test_two_phase_audit_e2e_uses_step_parameter(self):
        """规则: 两阶段审计 E2E 测试必须使用 require_profile(FULL, step='db_invariants')

        这确保 FULL profile 缺能力时触发 [FULL profile] 前缀的失败消息，
        便于 grep 定位和回归测试验证。
        """
        test_file = PROJECT_ROOT / "tests/gateway/test_two_phase_audit_e2e.py"
        content = test_file.read_text()

        # 验证使用了 step 参数（而非仅 ProfileType.FULL）
        assert 'require_profile(ProfileType.FULL, step="db_invariants")' in content, (
            "test_two_phase_audit_e2e.py 应使用 require_profile(ProfileType.FULL, step='db_invariants')，"
            "以确保 FULL 缺能力时触发 [FULL profile] 前缀"
        )

    def test_reconcile_outbox_uses_step_parameter(self):
        """规则: Reconcile Outbox 测试必须使用 require_profile(FULL, step='db_invariants')

        这确保 FULL profile 缺能力时触发 [FULL profile] 前缀的失败消息，
        便于 grep 定位和回归测试验证。
        """
        test_file = PROJECT_ROOT / "tests/gateway/test_reconcile_outbox.py"
        content = test_file.read_text()

        # 验证使用了 step 参数
        assert 'require_profile(ProfileType.FULL, step="db_invariants")' in content, (
            "test_reconcile_outbox.py 应使用 require_profile(ProfileType.FULL, step='db_invariants')，"
            "以确保 FULL 缺能力时触发 [FULL profile] 前缀"
        )

        # 验证所有 FULL profile 调用都使用了 step 参数
        import re

        full_calls = re.findall(r"require_profile\(ProfileType\.FULL[^)]*\)", content)
        for call in full_calls:
            assert "step=" in call, (
                f"发现未指定 step 参数的 require_profile 调用: {call}\n"
                "所有 FULL profile 调用应指定 step 参数以确保能力检查"
            )

    def test_http_only_mode_var_constant(self):
        """验证 HTTP_ONLY_MODE_VAR 常量存在"""
        test_file = PROJECT_ROOT / "tests/gateway/test_unified_stack_integration.py"
        content = test_file.read_text()

        # 验证环境变量名常量
        assert "HTTP_ONLY_MODE_VAR" in content or "HTTP_ONLY_MODE" in content, (
            "HTTP_ONLY_MODE 环境变量检测代码缺失"
        )

    def test_skip_degradation_var_constant(self):
        """验证 SKIP_DEGRADATION_VAR 常量存在"""
        test_file = PROJECT_ROOT / "tests/gateway/test_unified_stack_integration.py"
        content = test_file.read_text()

        # 验证环境变量名常量
        assert "SKIP_DEGRADATION" in content, "SKIP_DEGRADATION_TEST 环境变量检测代码缺失"


class TestNoRootWrappersDocumentationConsistency:
    """no_root_wrappers 文档一致性检查

    验证关键声明在多个文档中保持一致，防止文档漂移。
    """

    def test_deprecated_vs_preserved_section_exists_in_exceptions_doc(self):
        """验证 no_root_wrappers_exceptions.md 包含 deprecated vs preserved 治理差异章节"""
        doc_file = PROJECT_ROOT / "docs/architecture/no_root_wrappers_exceptions.md"
        content = doc_file.read_text()

        # 验证章节存在
        assert "Deprecated vs Preserved" in content, (
            "no_root_wrappers_exceptions.md 缺少 'Deprecated vs Preserved 的治理差异' 章节"
        )
        # 验证关键表格存在
        assert "deprecated: true" in content, (
            "no_root_wrappers_exceptions.md 缺少 deprecated: true 的说明"
        )
        assert "deprecated: false" in content, (
            "no_root_wrappers_exceptions.md 缺少 deprecated: false 的说明"
        )

    def test_allowlist_usage_section_exists_in_migration_map(self):
        """验证 no_root_wrappers_migration_map.md 包含 allowlist 用途说明"""
        doc_file = PROJECT_ROOT / "docs/architecture/no_root_wrappers_migration_map.md"
        content = doc_file.read_text()

        # 验证 allowlist 用途章节
        assert "Allowlist 用途与治理规则" in content or "Allowlist 的真实用途" in content, (
            "no_root_wrappers_migration_map.md 缺少 Allowlist 用途说明"
        )
        # 验证 preserved 模块不需要 allowlist 的说明
        assert "不需要 allowlist" in content or "无需 allowlist" in content, (
            "no_root_wrappers_migration_map.md 缺少 preserved 模块不需要 allowlist 的说明"
        )

    def test_cross_references_between_docs(self):
        """验证两个文档之间存在交叉引用"""
        exceptions_doc = PROJECT_ROOT / "docs/architecture/no_root_wrappers_exceptions.md"
        migration_doc = PROJECT_ROOT / "docs/architecture/no_root_wrappers_migration_map.md"

        exceptions_content = exceptions_doc.read_text()
        migration_content = migration_doc.read_text()

        # exceptions 文档应引用 migration_map
        assert "no_root_wrappers_migration_map.md" in exceptions_content, (
            "no_root_wrappers_exceptions.md 应引用 no_root_wrappers_migration_map.md"
        )
        # migration_map 文档应引用 exceptions
        assert "no_root_wrappers_exceptions.md" in migration_content, (
            "no_root_wrappers_migration_map.md 应引用 no_root_wrappers_exceptions.md"
        )

    def test_ci_runbook_references_both_docs(self):
        """验证 CI Runbook 引用了两个关键文档"""
        runbook = PROJECT_ROOT / "docs/dev/ci_gate_runbook.md"
        content = runbook.read_text()

        assert "no_root_wrappers_migration_map.md" in content, (
            "ci_gate_runbook.md 应引用 no_root_wrappers_migration_map.md"
        )
        assert "no_root_wrappers_exceptions.md" in content, (
            "ci_gate_runbook.md 应引用 no_root_wrappers_exceptions.md"
        )

    def test_preserved_modules_listed_correctly(self):
        """验证 preserved 模块（db, kv, artifacts）在文档中被正确标识"""
        migration_doc = PROJECT_ROOT / "docs/architecture/no_root_wrappers_migration_map.md"
        content = migration_doc.read_text()

        preserved_modules = ["db", "kv", "artifacts"]
        for module in preserved_modules:
            # 在长期保留模块章节中应该出现
            assert module in content, (
                f"preserved 模块 '{module}' 应在 no_root_wrappers_migration_map.md 中说明"
            )


class TestGateContractProfileInferencePriority:
    """Profile 推断优先级测试

    验证 get_profile_from_env() 函数遵循以下优先级：
    GATE_PROFILE > HTTP_ONLY_MODE > SKIP_DEGRADATION_TEST > default
    """

    def test_gate_profile_takes_highest_priority(self):
        """规则: GATE_PROFILE 显式指定时优先级最高"""
        import os
        from unittest.mock import patch

        # 从包模块导入
        from engram.unified_stack.gate_contract import ProfileType, get_profile_from_env

        # 即使其他变量也设置，GATE_PROFILE 优先
        with patch.dict(
            os.environ,
            {
                "GATE_PROFILE": "full",
                "HTTP_ONLY_MODE": "1",
                "SKIP_DEGRADATION_TEST": "1",
            },
            clear=False,
        ):
            profile = get_profile_from_env()
            assert profile == ProfileType.FULL, "GATE_PROFILE=full 应优先于其他变量"

    def test_http_only_mode_priority_over_skip_degradation(self):
        """规则: HTTP_ONLY_MODE 优先于 SKIP_DEGRADATION_TEST"""
        import os
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import ProfileType, get_profile_from_env

        # 无 GATE_PROFILE 时，HTTP_ONLY_MODE 优先于 SKIP_DEGRADATION_TEST
        with patch.dict(
            os.environ,
            {
                "GATE_PROFILE": "",
                "HTTP_ONLY_MODE": "1",
                "SKIP_DEGRADATION_TEST": "1",
            },
            clear=False,
        ):
            profile = get_profile_from_env()
            assert profile == ProfileType.HTTP_ONLY, (
                "HTTP_ONLY_MODE=1 应优先于 SKIP_DEGRADATION_TEST=1"
            )

    def test_skip_degradation_implies_standard(self):
        """规则: SKIP_DEGRADATION_TEST=1 时默认为 standard profile"""
        import os
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import ProfileType, get_profile_from_env

        with patch.dict(
            os.environ,
            {
                "GATE_PROFILE": "",
                "HTTP_ONLY_MODE": "",
                "SKIP_DEGRADATION_TEST": "1",
            },
            clear=False,
        ):
            profile = get_profile_from_env()
            assert profile == ProfileType.STANDARD, (
                "SKIP_DEGRADATION_TEST=1 应推断为 standard profile"
            )

    def test_default_profile_is_standard(self):
        """规则: 无任何环境变量时默认为 standard profile"""
        import os
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import ProfileType, get_profile_from_env

        with patch.dict(
            os.environ,
            {
                "GATE_PROFILE": "",
                "HTTP_ONLY_MODE": "",
                "SKIP_DEGRADATION_TEST": "",
            },
            clear=False,
        ):
            profile = get_profile_from_env()
            assert profile == ProfileType.STANDARD, "无环境变量时应默认为 standard"

    def test_gate_profile_httponly_alias(self):
        """规则: GATE_PROFILE 支持 httponly 别名（无下划线）"""
        import os
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import ProfileType, get_profile_from_env

        with patch.dict(os.environ, {"GATE_PROFILE": "httponly"}, clear=False):
            profile = get_profile_from_env()
            assert profile == ProfileType.HTTP_ONLY, "GATE_PROFILE=httponly 应识别为 http_only"


class TestComposeFileDetection:
    """Compose 文件识别测试

    验证 _check_compose_configured() 函数：
    - docker-compose.unified.yml 可被识别
    - ENGRAM_COMPOSE_FILE 环境变量可覆盖默认检测
    """

    def test_unified_compose_file_recognized(self):
        """规则: docker-compose.unified.yml 应在候选列表中"""
        import os
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import _check_compose_configured

        # 清除 ENGRAM_COMPOSE_FILE 以测试默认检测
        with patch.dict(os.environ, {"ENGRAM_COMPOSE_FILE": ""}, clear=False):
            # 检查 docker-compose.unified.yml 是否存在于项目根目录
            compose_path = PROJECT_ROOT / "docker-compose.unified.yml"
            if compose_path.exists():
                available, msg = _check_compose_configured()
                assert available is True, f"docker-compose.unified.yml 存在但未被识别: {msg}"
                assert "docker-compose.unified.yml" in msg, "返回消息应包含文件名"

    def test_engram_compose_file_env_overrides(self):
        """规则: ENGRAM_COMPOSE_FILE 环境变量可覆盖默认检测"""
        import os
        import tempfile
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import _check_compose_configured

        # 创建临时 compose 文件
        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
            f.write(b"version: '3'\nservices:\n  test:\n    image: test")
            temp_compose = f.name

        try:
            with patch.dict(os.environ, {"ENGRAM_COMPOSE_FILE": temp_compose}, clear=False):
                available, msg = _check_compose_configured()
                assert available is True, f"ENGRAM_COMPOSE_FILE 指定的文件应被识别: {msg}"
                assert "ENGRAM_COMPOSE_FILE" in msg, "返回消息应说明使用了环境变量"
        finally:
            os.unlink(temp_compose)

    def test_engram_compose_file_not_found_error(self):
        """规则: ENGRAM_COMPOSE_FILE 指定的文件不存在时返回错误"""
        import os
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import _check_compose_configured

        with patch.dict(os.environ, {"ENGRAM_COMPOSE_FILE": "/nonexistent/path.yml"}, clear=False):
            available, msg = _check_compose_configured()
            assert available is False, "不存在的文件应返回 False"
            assert "not found" in msg.lower(), "错误消息应说明文件未找到"


class TestOpenMemoryEndpointAlias:
    """OpenMemory URL/endpoint alias 测试

    验证 openmemory_endpoint_present capability：
    任一变量（OPENMEMORY_BASE_URL 或 OPENMEMORY_ENDPOINT）存在时 capability 为 true
    """

    def test_base_url_var_sufficient(self):
        """规则: OPENMEMORY_BASE_URL 存在即满足 capability"""
        import os
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import detect_capabilities

        with patch.dict(
            os.environ,
            {
                "OPENMEMORY_BASE_URL": "http://localhost:8080",
                "OPENMEMORY_ENDPOINT": "",
            },
            clear=False,
        ):
            report = detect_capabilities()
            status = report.capabilities.get("openmemory_endpoint_present")
            assert status is not None, "openmemory_endpoint_present capability 应存在"
            assert status.available is True, "OPENMEMORY_BASE_URL 存在时 capability 应为 true"
            assert "OPENMEMORY_BASE_URL" in status.message, "消息应说明使用了哪个变量"

    def test_endpoint_var_sufficient(self):
        """规则: OPENMEMORY_ENDPOINT 存在即满足 capability"""
        import os
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import detect_capabilities

        with patch.dict(
            os.environ,
            {
                "OPENMEMORY_BASE_URL": "",
                "OPENMEMORY_ENDPOINT": "http://localhost:8080",
            },
            clear=False,
        ):
            report = detect_capabilities()
            status = report.capabilities.get("openmemory_endpoint_present")
            assert status is not None, "openmemory_endpoint_present capability 应存在"
            assert status.available is True, "OPENMEMORY_ENDPOINT 存在时 capability 应为 true"
            assert "OPENMEMORY_ENDPOINT" in status.message, "消息应说明使用了哪个变量"

    def test_base_url_takes_priority(self):
        """规则: OPENMEMORY_BASE_URL 优先于 OPENMEMORY_ENDPOINT"""
        import os
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import detect_capabilities

        with patch.dict(
            os.environ,
            {
                "OPENMEMORY_BASE_URL": "http://primary:8080",
                "OPENMEMORY_ENDPOINT": "http://fallback:8080",
            },
            clear=False,
        ):
            report = detect_capabilities()
            status = report.capabilities.get("openmemory_endpoint_present")
            assert status.available is True
            # 优先使用 OPENMEMORY_BASE_URL
            assert "OPENMEMORY_BASE_URL" in status.message, "应优先使用 OPENMEMORY_BASE_URL"
            assert "primary" in status.message, "消息应包含优先变量的值"

    def test_neither_var_set_returns_false(self):
        """规则: 两个变量都未设置时 capability 为 false"""
        import os
        from unittest.mock import patch

        from engram.unified_stack.gate_contract import detect_capabilities

        with patch.dict(
            os.environ,
            {
                "OPENMEMORY_BASE_URL": "",
                "OPENMEMORY_ENDPOINT": "",
            },
            clear=False,
        ):
            report = detect_capabilities()
            status = report.capabilities.get("openmemory_endpoint_present")
            assert status.available is False, "无变量时 capability 应为 false"


class TestFullProfileMustFailSteps:
    """FULL profile must_fail_if_blocked 固定测试

    验证 FULL profile 的 must_fail_if_blocked 必须包含：
    - degradation
    - db_invariants
    """

    def test_full_profile_must_fail_contains_degradation(self):
        """规则: FULL profile must_fail_if_blocked 必须包含 degradation"""
        from engram.unified_stack.gate_contract import PROFILE_CONFIGS, ProfileType, StepName

        full_config = PROFILE_CONFIGS.get(ProfileType.FULL)
        assert full_config is not None, "FULL profile 配置应存在"

        must_fail_steps = {s.value for s in full_config.must_fail_if_blocked}
        assert StepName.DEGRADATION.value in must_fail_steps, (
            f"FULL profile must_fail_if_blocked 必须包含 degradation，当前: {must_fail_steps}"
        )

    def test_full_profile_must_fail_contains_db_invariants(self):
        """规则: FULL profile must_fail_if_blocked 必须包含 db_invariants"""
        from engram.unified_stack.gate_contract import PROFILE_CONFIGS, ProfileType, StepName

        full_config = PROFILE_CONFIGS.get(ProfileType.FULL)
        assert full_config is not None, "FULL profile 配置应存在"

        must_fail_steps = {s.value for s in full_config.must_fail_if_blocked}
        assert StepName.DB_INVARIANTS.value in must_fail_steps, (
            f"FULL profile must_fail_if_blocked 必须包含 db_invariants，当前: {must_fail_steps}"
        )

    def test_full_profile_must_fail_steps_are_exactly_expected(self):
        """规则: FULL profile must_fail_if_blocked 固定为 {degradation, db_invariants}"""
        from engram.unified_stack.gate_contract import PROFILE_CONFIGS, ProfileType, StepName

        full_config = PROFILE_CONFIGS.get(ProfileType.FULL)
        assert full_config is not None

        must_fail_steps = {s.value for s in full_config.must_fail_if_blocked}
        expected = {StepName.DEGRADATION.value, StepName.DB_INVARIANTS.value}

        assert must_fail_steps == expected, (
            f"FULL profile must_fail_if_blocked 应恰好为 {expected}，当前: {must_fail_steps}"
        )

    def test_ssot_consistency_with_test_file_fallback(self):
        """规则: SSoT 与测试文件回退常量保持一致"""
        # 读取测试文件中的回退常量
        test_file = PROJECT_ROOT / "tests/gateway/test_unified_stack_integration.py"
        content = test_file.read_text()

        # 验证回退常量包含预期步骤
        assert "_FALLBACK_MUST_FAIL_STEPS" in content, "测试文件应包含回退常量"
        assert '"degradation"' in content or "'degradation'" in content, (
            "回退常量应包含 degradation"
        )
        assert '"db_invariants"' in content or "'db_invariants'" in content, (
            "回退常量应包含 db_invariants"
        )


class TestMakefileTargetConsistency:
    """Makefile 目标与文档一致性检查"""

    def test_documented_targets_status(self):
        """
        检查文档中描述的 Makefile 目标实现状态

        注意: 此测试不强制要求所有目标都实现，
        而是确保文档中标记为"已实现"的目标确实存在。
        """
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 必须存在的目标（文档标记为已实现）
        required_targets = [
            "verify-unified:",
            "test-gateway:",
        ]

        for target in required_targets:
            assert target in content, f"Makefile 缺少必需目标: {target.rstrip(':')}"

    def test_acceptance_targets_note(self):
        """
        验证 acceptance 相关目标的文档注释

        当前 acceptance-unified-min/full 目标尚未在 Makefile 实现，
        此测试确保如果实现了，变量绑定必须正确。
        """
        makefile = PROJECT_ROOT / "Makefile"
        content = makefile.read_text()

        # 如果 acceptance-unified-min 存在，验证环境变量
        if "acceptance-unified-min:" in content:
            # 如果实现了，必须设置 HTTP_ONLY_MODE=1
            assert "HTTP_ONLY_MODE" in content, "acceptance-unified-min 应设置 HTTP_ONLY_MODE"

        # 如果 acceptance-unified-full 存在，验证环境变量
        if "acceptance-unified-full:" in content:
            # 如果实现了，必须设置 VERIFY_FULL=1
            assert "VERIFY_FULL" in content, "acceptance-unified-full 应设置 VERIFY_FULL"
