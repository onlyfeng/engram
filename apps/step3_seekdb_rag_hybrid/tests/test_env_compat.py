#!/usr/bin/env python3
"""
test_env_compat.py - 环境变量兼容层测试

测试内容：
1. get_bool: 边界值与大小写
2. get_choice: 边界值、大小写、废弃值别名
3. PGVectorConfig.from_env() 的 legacy 变量支持
4. MigrationConfig.from_env() 和 InspectConfig.from_env() 的 canonical/legacy 优先级
5. 冲突检测
"""

import os
import sys
import warnings
from pathlib import Path

import pytest

# 添加父目录到路径
_step3_path = Path(__file__).parent.parent
if str(_step3_path) not in sys.path:
    sys.path.insert(0, str(_step3_path))

# 添加 scripts 目录到路径
_scripts_path = _step3_path / "scripts"
if str(_scripts_path) not in sys.path:
    sys.path.insert(0, str(_scripts_path))

from step3_seekdb_rag_hybrid.env_compat import (
    get_str,
    get_int,
    get_bool,
    get_choice,
    EnvConflictError,
    set_allow_conflict,
    reset_deprecation_warnings,
)


# ============ Fixtures ============


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """每个测试前清理环境变量和全局状态"""
    # 清理可能存在的环境变量
    env_vars_to_clear = [
        "STEP3_PG_SCHEMA", "STEP3_SCHEMA",
        "STEP3_PG_TABLE", "STEP3_TABLE",
        "STEP3_PGVECTOR_COLLECTION_STRATEGY",
        "STEP3_PGVECTOR_DSN",
        "STEP3_ENV_ALLOW_CONFLICT",
        "TEST_VAR", "TEST_VAR_LEGACY", "TEST_BOOL", "TEST_CHOICE",
        "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
        "POSTGRES_USER", "POSTGRES_PASSWORD",
        "STEP3_PG_HOST", "STEP3_PG_PORT", "STEP3_PG_DB",
        "STEP3_PG_USER", "STEP3_PG_PASSWORD",
        "PGVECTOR_DSN",
    ]
    for var in env_vars_to_clear:
        monkeypatch.delenv(var, raising=False)
    
    # 重置全局状态
    reset_deprecation_warnings()
    set_allow_conflict(False)
    
    yield


# ============ get_bool 边界值与大小写测试 ============


class TestGetBool:
    """get_bool 函数测试"""

    def test_true_values(self, monkeypatch):
        """测试所有真值"""
        true_values = ["1", "true", "yes", "on", "enabled"]
        
        for val in true_values:
            monkeypatch.setenv("TEST_BOOL", val)
            result = get_bool("TEST_BOOL")
            assert result is True, f"'{val}' should be True"

    def test_true_values_uppercase(self, monkeypatch):
        """测试真值大写"""
        true_values = ["TRUE", "YES", "ON", "ENABLED"]
        
        for val in true_values:
            monkeypatch.setenv("TEST_BOOL", val)
            result = get_bool("TEST_BOOL")
            assert result is True, f"'{val}' should be True"

    def test_true_values_mixed_case(self, monkeypatch):
        """测试真值混合大小写"""
        true_values = ["True", "Yes", "On", "Enabled", "TrUe"]
        
        for val in true_values:
            monkeypatch.setenv("TEST_BOOL", val)
            result = get_bool("TEST_BOOL")
            assert result is True, f"'{val}' should be True"

    def test_false_values(self, monkeypatch):
        """测试所有假值"""
        false_values = ["0", "false", "no", "off", "disabled", ""]
        
        for val in false_values:
            monkeypatch.setenv("TEST_BOOL", val)
            result = get_bool("TEST_BOOL")
            assert result is False, f"'{val}' should be False"

    def test_false_values_uppercase(self, monkeypatch):
        """测试假值大写"""
        false_values = ["FALSE", "NO", "OFF", "DISABLED"]
        
        for val in false_values:
            monkeypatch.setenv("TEST_BOOL", val)
            result = get_bool("TEST_BOOL")
            assert result is False, f"'{val}' should be False"

    def test_invalid_value_raises(self, monkeypatch):
        """测试无效值抛出异常"""
        monkeypatch.setenv("TEST_BOOL", "invalid")
        
        with pytest.raises(ValueError) as exc_info:
            get_bool("TEST_BOOL")
        
        assert "invalid" in str(exc_info.value)
        assert "TEST_BOOL" in str(exc_info.value)

    def test_default_value_when_not_set(self):
        """测试环境变量不存在时使用默认值"""
        assert get_bool("TEST_BOOL", default=True) is True
        assert get_bool("TEST_BOOL", default=False) is False
        assert get_bool("TEST_BOOL", default=None) is None

    def test_cli_value_takes_precedence(self, monkeypatch):
        """测试 CLI 值优先于环境变量"""
        monkeypatch.setenv("TEST_BOOL", "true")
        
        result = get_bool("TEST_BOOL", cli_value=False)
        assert result is False

    def test_whitespace_trimmed(self, monkeypatch):
        """测试值两端空白被正确处理"""
        monkeypatch.setenv("TEST_BOOL", "  true  ")
        result = get_bool("TEST_BOOL")
        assert result is True

    def test_value_aliases(self, monkeypatch):
        """测试自定义值别名"""
        monkeypatch.setenv("TEST_BOOL", "enable")
        
        result = get_bool(
            "TEST_BOOL",
            value_aliases={"enable": True, "disable": False}
        )
        assert result is True


# ============ get_choice 边界值与大小写测试 ============


class TestGetChoice:
    """get_choice 函数测试"""

    def test_valid_choice(self, monkeypatch):
        """测试有效选项"""
        monkeypatch.setenv("TEST_CHOICE", "option_a")
        
        result = get_choice(
            "TEST_CHOICE",
            choices=["option_a", "option_b", "option_c"]
        )
        assert result == "option_a"

    def test_case_insensitive_by_default(self, monkeypatch):
        """测试默认大小写不敏感"""
        monkeypatch.setenv("TEST_CHOICE", "OPTION_A")
        
        result = get_choice(
            "TEST_CHOICE",
            choices=["option_a", "option_b"]
        )
        # 返回原始 choices 中的值（保持大小写）
        assert result == "option_a"

    def test_case_sensitive_mode(self, monkeypatch):
        """测试大小写敏感模式"""
        monkeypatch.setenv("TEST_CHOICE", "OPTION_A")
        
        with pytest.raises(ValueError):
            get_choice(
                "TEST_CHOICE",
                choices=["option_a", "option_b"],
                case_sensitive=True
            )

    def test_invalid_choice_raises(self, monkeypatch):
        """测试无效选项抛出异常"""
        monkeypatch.setenv("TEST_CHOICE", "invalid_option")
        
        with pytest.raises(ValueError) as exc_info:
            get_choice(
                "TEST_CHOICE",
                choices=["option_a", "option_b"]
            )
        
        assert "invalid_option" in str(exc_info.value)
        assert "option_a" in str(exc_info.value)

    def test_value_aliases(self, monkeypatch):
        """测试值别名"""
        monkeypatch.setenv("TEST_CHOICE", "st")
        
        result = get_choice(
            "TEST_CHOICE",
            choices=["single_table", "per_table"],
            value_aliases={"st": "single_table", "pt": "per_table"}
        )
        assert result == "single_table"

    def test_deprecated_value_aliases_with_warning(self, monkeypatch):
        """测试废弃值别名触发警告"""
        monkeypatch.setenv("TEST_CHOICE", "single")
        reset_deprecation_warnings()
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = get_choice(
                "TEST_CHOICE",
                choices=["single_table", "per_table"],
                deprecated_value_aliases={"single": "single_table"}
            )
        
        assert result == "single_table"
        # 验证有废弃警告
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) > 0

    def test_default_value_when_not_set(self):
        """测试环境变量不存在时使用默认值"""
        result = get_choice(
            "TEST_CHOICE",
            choices=["option_a", "option_b"],
            default="option_b"
        )
        assert result == "option_b"

    def test_whitespace_trimmed(self, monkeypatch):
        """测试值两端空白被正确处理"""
        monkeypatch.setenv("TEST_CHOICE", "  option_a  ")
        
        result = get_choice(
            "TEST_CHOICE",
            choices=["option_a", "option_b"]
        )
        assert result == "option_a"


# ============ PGVectorConfig.from_env() 测试 ============


class TestPGVectorConfigFromEnv:
    """PGVectorConfig.from_env() 测试"""

    def test_legacy_only_schema_with_warning(self, monkeypatch):
        """测试仅设置 legacy STEP3_SCHEMA 时能正确解析并告警"""
        from step3_backend_factory import PGVectorConfig
        
        monkeypatch.setenv("STEP3_SCHEMA", "legacy_schema")
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
        reset_deprecation_warnings()
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = PGVectorConfig.from_env()
        
        # 验证值被正确读取
        assert config.schema == "legacy_schema"
        
        # 验证触发废弃警告
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) > 0
        assert "STEP3_SCHEMA" in str(deprecation_warnings[0].message)

    def test_legacy_only_table_with_warning(self, monkeypatch):
        """测试仅设置 legacy STEP3_TABLE 时能正确解析并告警"""
        from step3_backend_factory import PGVectorConfig
        
        monkeypatch.setenv("STEP3_TABLE", "legacy_table")
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
        reset_deprecation_warnings()
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = PGVectorConfig.from_env()
        
        assert config.table == "legacy_table"
        
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) > 0
        assert "STEP3_TABLE" in str(deprecation_warnings[0].message)

    def test_canonical_takes_precedence_over_legacy(self, monkeypatch):
        """测试 canonical 优先于 legacy"""
        from step3_backend_factory import PGVectorConfig
        
        # 同时设置 canonical 和 legacy，值一致
        monkeypatch.setenv("STEP3_PG_SCHEMA", "canonical_schema")
        monkeypatch.setenv("STEP3_SCHEMA", "canonical_schema")  # 一致
        monkeypatch.setenv("STEP3_PG_TABLE", "canonical_table")
        monkeypatch.setenv("STEP3_TABLE", "canonical_table")  # 一致
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
        
        config = PGVectorConfig.from_env()
        
        assert config.schema == "canonical_schema"
        assert config.table == "canonical_table"

    def test_conflict_when_canonical_and_legacy_differ(self, monkeypatch):
        """测试 canonical 与 legacy 值不一致时报错"""
        from step3_backend_factory import PGVectorConfig
        
        monkeypatch.setenv("STEP3_PG_SCHEMA", "canonical_schema")
        monkeypatch.setenv("STEP3_SCHEMA", "different_schema")  # 不一致
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
        set_allow_conflict(False)
        reset_deprecation_warnings()
        
        with pytest.raises(EnvConflictError) as exc_info:
            PGVectorConfig.from_env()
        
        error = exc_info.value
        assert error.canonical == "STEP3_PG_SCHEMA"
        assert error.legacy == "STEP3_SCHEMA"
        assert error.canonical_value == "canonical_schema"
        assert error.legacy_value == "different_schema"

    def test_conflict_allowed_with_warning(self, monkeypatch):
        """测试允许冲突时仅警告"""
        from step3_backend_factory import PGVectorConfig
        import step3_seekdb_rag_hybrid.env_compat as env_compat
        env_compat._allow_conflict = None  # 重置让环境变量生效
        env_compat._warned_deprecated = set()
        env_compat._warned_deprecated_values = set()
        
        monkeypatch.setenv("STEP3_PG_SCHEMA", "canonical_schema")
        monkeypatch.setenv("STEP3_SCHEMA", "different_schema")  # 不一致
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
        monkeypatch.setenv("STEP3_ENV_ALLOW_CONFLICT", "1")
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = PGVectorConfig.from_env()
        
        # 应使用 canonical 值
        assert config.schema == "canonical_schema"
        
        # 应有冲突警告
        user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
        assert len(user_warnings) > 0

    def test_collection_strategy_legacy_value_single(self, monkeypatch):
        """测试 STEP3_PGVECTOR_COLLECTION_STRATEGY legacy 值 'single' 映射到 'single_table'"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_SINGLE_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "single")
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
        reset_deprecation_warnings()
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = PGVectorConfig.from_env()
        
        assert config.collection_strategy == COLLECTION_STRATEGY_SINGLE_TABLE
        
        # 验证触发废弃警告
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) > 0

    def test_collection_strategy_legacy_value_shared(self, monkeypatch):
        """测试 legacy 值 'shared' 映射到 'single_table'"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_SINGLE_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "shared")
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
        reset_deprecation_warnings()
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = PGVectorConfig.from_env()
        
        assert config.collection_strategy == COLLECTION_STRATEGY_SINGLE_TABLE

    def test_collection_strategy_legacy_value_per_project(self, monkeypatch):
        """测试 legacy 值 'per_project' 映射到 'per_table'"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_PER_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "per_project")
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
        reset_deprecation_warnings()
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = PGVectorConfig.from_env()
        
        assert config.collection_strategy == COLLECTION_STRATEGY_PER_TABLE

    def test_collection_strategy_legacy_value_per_collection(self, monkeypatch):
        """测试 legacy 值 'per-collection' 映射到 'per_table'"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_PER_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", "per-collection")
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
        reset_deprecation_warnings()
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = PGVectorConfig.from_env()
        
        assert config.collection_strategy == COLLECTION_STRATEGY_PER_TABLE

    def test_collection_strategy_canonical_values(self, monkeypatch):
        """测试 canonical 策略值不触发警告"""
        from step3_backend_factory import (
            PGVectorConfig,
            COLLECTION_STRATEGY_PER_TABLE,
            COLLECTION_STRATEGY_SINGLE_TABLE,
            COLLECTION_STRATEGY_ROUTING,
        )
        
        canonical_values = [
            (COLLECTION_STRATEGY_PER_TABLE, "per_table"),
            (COLLECTION_STRATEGY_SINGLE_TABLE, "single_table"),
            (COLLECTION_STRATEGY_ROUTING, "routing"),
        ]
        
        for expected, value in canonical_values:
            monkeypatch.setenv("STEP3_PGVECTOR_COLLECTION_STRATEGY", value)
            monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
            reset_deprecation_warnings()
            
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                config = PGVectorConfig.from_env()
            
            assert config.collection_strategy == expected
            # canonical 值不应触发 DeprecationWarning（仅值相关的）
            deprecation_warnings = [
                x for x in w 
                if issubclass(x.category, DeprecationWarning) 
                and value in str(x.message)
            ]
            assert len(deprecation_warnings) == 0, f"'{value}' should not trigger deprecation warning"

    def test_default_values(self, monkeypatch):
        """测试默认值"""
        from step3_backend_factory import PGVectorConfig, COLLECTION_STRATEGY_SINGLE_TABLE
        
        monkeypatch.setenv("STEP3_PGVECTOR_DSN", "postgresql://test:test@localhost:5432/test")
        
        config = PGVectorConfig.from_env()
        
        assert config.schema == "step3"
        assert config.table == "chunks"
        assert config.collection_strategy == COLLECTION_STRATEGY_SINGLE_TABLE


# ============ MigrationConfig.from_env() 测试 ============


class TestMigrationConfigFromEnv:
    """MigrationConfig.from_env() 测试"""

    def test_canonical_schema_takes_precedence(self, monkeypatch):
        """测试 STEP3_PG_SCHEMA 优先于 STEP3_SCHEMA"""
        from pgvector_collection_migrate import MigrationConfig
        # 需要重置 scripts 中的 env_compat 状态（它们使用不同的导入路径）
        import env_compat as scripts_env_compat
        scripts_env_compat._allow_conflict = None
        scripts_env_compat._warned_deprecated = set()
        
        monkeypatch.setenv("STEP3_PG_SCHEMA", "canonical_schema")
        monkeypatch.setenv("STEP3_SCHEMA", "legacy_schema")
        monkeypatch.setenv("POSTGRES_PASSWORD", "test")
        monkeypatch.setenv("STEP3_ENV_ALLOW_CONFLICT", "1")  # 通过环境变量允许冲突
        
        config = MigrationConfig.from_env()
        
        assert config.schema == "canonical_schema"

    def test_canonical_table_takes_precedence(self, monkeypatch):
        """测试 STEP3_PG_TABLE 优先于 STEP3_TABLE"""
        from pgvector_collection_migrate import MigrationConfig
        import env_compat as scripts_env_compat
        scripts_env_compat._allow_conflict = None
        scripts_env_compat._warned_deprecated = set()
        
        monkeypatch.setenv("STEP3_PG_TABLE", "canonical_table")
        monkeypatch.setenv("STEP3_TABLE", "legacy_table")
        monkeypatch.setenv("POSTGRES_PASSWORD", "test")
        monkeypatch.setenv("STEP3_ENV_ALLOW_CONFLICT", "1")
        
        config = MigrationConfig.from_env()
        
        assert config.base_table == "canonical_table"

    def test_legacy_only_with_warning(self, monkeypatch):
        """测试仅设置 legacy 变量时能正确读取并告警"""
        from pgvector_collection_migrate import MigrationConfig
        import env_compat as scripts_env_compat
        scripts_env_compat._allow_conflict = None
        scripts_env_compat._warned_deprecated = set()
        scripts_env_compat._warned_deprecated_values = set()
        
        monkeypatch.setenv("STEP3_SCHEMA", "legacy_schema")
        monkeypatch.setenv("STEP3_TABLE", "legacy_table")
        monkeypatch.setenv("POSTGRES_PASSWORD", "test")
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = MigrationConfig.from_env()
        
        assert config.schema == "legacy_schema"
        assert config.base_table == "legacy_table"
        
        # 验证触发废弃警告
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) > 0


# ============ InspectConfig.from_env() 测试 ============


class TestInspectConfigFromEnv:
    """InspectConfig.from_env() 测试"""

    def test_canonical_schema_takes_precedence(self, monkeypatch):
        """测试 STEP3_PG_SCHEMA 优先于 STEP3_SCHEMA"""
        from pgvector_inspect import InspectConfig
        import env_compat as scripts_env_compat
        scripts_env_compat._allow_conflict = None
        scripts_env_compat._warned_deprecated = set()
        
        monkeypatch.setenv("STEP3_PG_SCHEMA", "canonical_schema")
        monkeypatch.setenv("STEP3_SCHEMA", "legacy_schema")
        monkeypatch.setenv("POSTGRES_PASSWORD", "test")
        monkeypatch.setenv("STEP3_ENV_ALLOW_CONFLICT", "1")
        
        config = InspectConfig.from_env()
        
        assert config.base_schema == "canonical_schema"

    def test_legacy_only_with_warning(self, monkeypatch):
        """测试仅设置 legacy 变量时能正确读取并告警"""
        from pgvector_inspect import InspectConfig
        import env_compat as scripts_env_compat
        scripts_env_compat._allow_conflict = None
        scripts_env_compat._warned_deprecated = set()
        scripts_env_compat._warned_deprecated_values = set()
        
        monkeypatch.setenv("STEP3_SCHEMA", "legacy_schema")
        monkeypatch.setenv("POSTGRES_PASSWORD", "test")
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = InspectConfig.from_env()
        
        assert config.base_schema == "legacy_schema"
        
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) > 0


# ============ 冲突检测测试 ============


class TestEnvConflict:
    """环境变量冲突检测测试"""

    def test_conflict_error_attributes(self, monkeypatch):
        """测试 EnvConflictError 包含正确的属性"""
        monkeypatch.setenv("TEST_VAR", "canonical_value")
        monkeypatch.setenv("TEST_VAR_LEGACY", "legacy_value")
        set_allow_conflict(False)
        reset_deprecation_warnings()
        
        with pytest.raises(EnvConflictError) as exc_info:
            get_str(
                "TEST_VAR",
                deprecated_aliases=["TEST_VAR_LEGACY"]
            )
        
        error = exc_info.value
        assert error.canonical == "TEST_VAR"
        assert error.legacy == "TEST_VAR_LEGACY"
        assert error.canonical_value == "canonical_value"
        assert error.legacy_value == "legacy_value"

    def test_no_conflict_when_values_match(self, monkeypatch):
        """测试值一致时不报错"""
        monkeypatch.setenv("TEST_VAR", "same_value")
        monkeypatch.setenv("TEST_VAR_LEGACY", "same_value")
        set_allow_conflict(False)
        reset_deprecation_warnings()
        
        # 不应抛出异常
        result = get_str(
            "TEST_VAR",
            deprecated_aliases=["TEST_VAR_LEGACY"]
        )
        assert result == "same_value"

    def test_allow_conflict_via_env(self, monkeypatch):
        """测试通过环境变量允许冲突"""
        monkeypatch.setenv("TEST_VAR", "canonical_value")
        monkeypatch.setenv("TEST_VAR_LEGACY", "legacy_value")
        monkeypatch.setenv("STEP3_ENV_ALLOW_CONFLICT", "1")
        reset_deprecation_warnings()
        
        # 重置 set_allow_conflict 的值，让环境变量生效
        import step3_seekdb_rag_hybrid.env_compat as env_compat
        env_compat._allow_conflict = None
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = get_str(
                "TEST_VAR",
                deprecated_aliases=["TEST_VAR_LEGACY"]
            )
        
        # 应使用 canonical 值
        assert result == "canonical_value"
        
        # 应有冲突警告
        user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
        assert len(user_warnings) > 0


# ============ get_str 基本测试 ============


class TestGetStr:
    """get_str 函数测试"""

    def test_basic_read(self, monkeypatch):
        """测试基本读取"""
        monkeypatch.setenv("TEST_VAR", "test_value")
        
        result = get_str("TEST_VAR")
        assert result == "test_value"

    def test_default_value(self):
        """测试默认值"""
        result = get_str("TEST_VAR", default="default_value")
        assert result == "default_value"

    def test_required_raises(self):
        """测试 required=True 时抛出异常"""
        with pytest.raises(ValueError) as exc_info:
            get_str("TEST_VAR", required=True)
        
        assert "TEST_VAR" in str(exc_info.value)

    def test_cli_value_precedence(self, monkeypatch):
        """测试 CLI 值优先"""
        monkeypatch.setenv("TEST_VAR", "env_value")
        
        result = get_str("TEST_VAR", cli_value="cli_value")
        assert result == "cli_value"


# ============ get_int 基本测试 ============


class TestGetInt:
    """get_int 函数测试"""

    def test_basic_read(self, monkeypatch):
        """测试基本读取"""
        monkeypatch.setenv("TEST_VAR", "42")
        
        result = get_int("TEST_VAR")
        assert result == 42

    def test_invalid_int_raises(self, monkeypatch):
        """测试无效整数抛出异常"""
        monkeypatch.setenv("TEST_VAR", "not_a_number")
        
        with pytest.raises(ValueError) as exc_info:
            get_int("TEST_VAR")
        
        assert "not_a_number" in str(exc_info.value)


# ============ 废弃警告测试 ============


class TestDeprecationWarnings:
    """废弃警告测试"""

    def test_warning_only_once(self, monkeypatch):
        """测试每个变量名只警告一次"""
        monkeypatch.setenv("TEST_VAR_LEGACY", "value")
        reset_deprecation_warnings()
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            # 多次调用
            get_str("TEST_VAR", deprecated_aliases=["TEST_VAR_LEGACY"])
            get_str("TEST_VAR", deprecated_aliases=["TEST_VAR_LEGACY"])
            get_str("TEST_VAR", deprecated_aliases=["TEST_VAR_LEGACY"])
        
        # 应只有一次废弃警告
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) == 1

    def test_warning_message_content(self, monkeypatch):
        """测试警告信息内容"""
        monkeypatch.setenv("TEST_VAR_LEGACY", "value")
        reset_deprecation_warnings()
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            get_str("TEST_VAR", deprecated_aliases=["TEST_VAR_LEGACY"])
        
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) > 0
        
        msg = str(deprecation_warnings[0].message)
        assert "TEST_VAR_LEGACY" in msg
        assert "TEST_VAR" in msg
        assert "废弃" in msg or "deprecated" in msg.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
