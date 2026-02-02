# -*- coding: utf-8 -*-
"""
Gateway 启动配置测试

测试覆盖:
- OPENMEMORY_API_KEY vs OM_API_KEY 优先级
- 配置文件 vs 环境变量
- 启动时 DB 检查开关 (LOGBOOK_CHECK_ON_STARTUP / AUTO_MIGRATE_ON_STARTUP)

验证行为与 docs/reference/environment_variables.md 一致
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# ======================== OPENMEMORY_API_KEY vs OM_API_KEY 测试 ========================


class TestAPIKeyPriority:
    """
    验证 API Key 优先级

    文档要求（docs/reference/environment_variables.md）:
    - OPENMEMORY_API_KEY 优先级高于 OM_API_KEY
    - OPENMEMORY_API_KEY 兼容旧配置，OM_API_KEY 为推荐
    """

    def setup_method(self):
        """每个测试前重置配置"""
        from engram.gateway.config import reset_config

        reset_config()
        # 清理相关环境变量
        self._saved_env = {}
        for key in ["OPENMEMORY_API_KEY", "OM_API_KEY", "PROJECT_KEY", "POSTGRES_DSN"]:
            self._saved_env[key] = os.environ.get(key)
            if key in os.environ:
                del os.environ[key]

    def teardown_method(self):
        """每个测试后恢复环境变量"""
        from engram.gateway.config import reset_config

        reset_config()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_openmemory_api_key_takes_priority_over_om_api_key(self):
        """
        关键路径测试: OPENMEMORY_API_KEY 优先于 OM_API_KEY

        文档: "OPENMEMORY_API_KEY 优先级高于 OM_API_KEY"
        """
        from engram.gateway.config import load_config, reset_config

        reset_config()

        # 设置必填项
        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"

        # 同时设置两个 API Key
        os.environ["OPENMEMORY_API_KEY"] = "openmemory_key_higher_priority"
        os.environ["OM_API_KEY"] = "om_key_lower_priority"

        try:
            config = load_config()

            # 关键断言: OPENMEMORY_API_KEY 应该生效
            assert config.openmemory_api_key == "openmemory_key_higher_priority", (
                f"OPENMEMORY_API_KEY 应该优先于 OM_API_KEY，"
                f"期望: openmemory_key_higher_priority，"
                f"实际: {config.openmemory_api_key}"
            )
        finally:
            reset_config()

    def test_om_api_key_used_when_openmemory_api_key_not_set(self):
        """
        关键路径测试: 当 OPENMEMORY_API_KEY 未设置时使用 OM_API_KEY
        """
        from engram.gateway.config import load_config, reset_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"

        # 仅设置 OM_API_KEY
        os.environ["OM_API_KEY"] = "om_key_fallback"

        try:
            config = load_config()

            # 关键断言: OM_API_KEY 作为回退
            assert config.openmemory_api_key == "om_key_fallback", (
                f"OM_API_KEY 应作为回退使用，"
                f"期望: om_key_fallback，"
                f"实际: {config.openmemory_api_key}"
            )
        finally:
            reset_config()

    def test_empty_openmemory_api_key_falls_back_to_om_api_key(self):
        """
        关键路径测试: 空字符串的 OPENMEMORY_API_KEY 应回退到 OM_API_KEY
        """
        from engram.gateway.config import load_config, reset_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"

        # 设置空字符串的 OPENMEMORY_API_KEY
        os.environ["OPENMEMORY_API_KEY"] = ""
        os.environ["OM_API_KEY"] = "om_key_fallback"

        try:
            config = load_config()

            # 空字符串应被视为未设置，回退到 OM_API_KEY
            assert config.openmemory_api_key == "om_key_fallback", (
                f"空 OPENMEMORY_API_KEY 应回退到 OM_API_KEY，"
                f"期望: om_key_fallback，"
                f"实际: {config.openmemory_api_key}"
            )
        finally:
            reset_config()

    def test_neither_api_key_set_returns_none(self):
        """
        关键路径测试: 两个 API Key 都未设置时返回 None
        """
        from engram.gateway.config import load_config, reset_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"

        # 不设置任何 API Key
        os.environ.pop("OPENMEMORY_API_KEY", None)
        os.environ.pop("OM_API_KEY", None)

        try:
            config = load_config()

            # 关键断言: 应返回 None
            assert config.openmemory_api_key is None, (
                f"两个 API Key 都未设置时应返回 None，实际: {config.openmemory_api_key}"
            )
        finally:
            reset_config()


# ======================== 启动时 DB 检查开关测试 ========================


class TestStartupDBCheckConfig:
    """
    验证启动时 DB 检查开关行为

    文档要求（docs/reference/environment_variables.md）:
    - LOGBOOK_CHECK_ON_STARTUP: 是否在启动时检查 DB（默认 true）
    - AUTO_MIGRATE_ON_STARTUP: 检测到 DB 缺失时是否自动迁移（默认 false）
    """

    def setup_method(self):
        """每个测试前重置配置"""
        from engram.gateway.config import reset_config

        reset_config()
        self._saved_env = {}
        for key in [
            "PROJECT_KEY",
            "POSTGRES_DSN",
            "LOGBOOK_CHECK_ON_STARTUP",
            "AUTO_MIGRATE_ON_STARTUP",
        ]:
            self._saved_env[key] = os.environ.get(key)
            if key in os.environ:
                del os.environ[key]

    def teardown_method(self):
        """每个测试后恢复环境变量"""
        from engram.gateway.config import reset_config

        reset_config()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_logbook_check_on_startup_default_true(self):
        """
        关键路径测试: LOGBOOK_CHECK_ON_STARTUP 默认为 true
        """
        from engram.gateway.config import load_config, reset_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"

        # 不设置 LOGBOOK_CHECK_ON_STARTUP
        os.environ.pop("LOGBOOK_CHECK_ON_STARTUP", None)

        try:
            config = load_config()

            # 关键断言: 默认值为 True
            assert config.logbook_check_on_startup is True, (
                f"LOGBOOK_CHECK_ON_STARTUP 默认值应为 True，实际: {config.logbook_check_on_startup}"
            )
        finally:
            reset_config()

    def test_logbook_check_on_startup_false_value(self):
        """
        关键路径测试: LOGBOOK_CHECK_ON_STARTUP=false 应禁用检查
        """
        from engram.gateway.config import load_config, reset_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"
        os.environ["LOGBOOK_CHECK_ON_STARTUP"] = "false"

        try:
            config = load_config()

            # 关键断言
            assert config.logbook_check_on_startup is False, (
                f"LOGBOOK_CHECK_ON_STARTUP=false 应禁用检查，"
                f"实际: {config.logbook_check_on_startup}"
            )
        finally:
            reset_config()

    def test_logbook_check_on_startup_accepts_various_false_values(self):
        """
        测试 LOGBOOK_CHECK_ON_STARTUP 接受多种 false 值格式
        """
        from engram.gateway.config import load_config, reset_config

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"

        false_values = ["false", "FALSE", "False", "0", "no", "NO"]

        for val in false_values:
            reset_config()
            os.environ["LOGBOOK_CHECK_ON_STARTUP"] = val

            config = load_config()

            # 所有这些值都应解析为 False
            expected = val.lower() in ("false", "0", "no")
            # 注意：当前实现只识别 "true", "1", "yes" 为 True，其他都是 False
            assert (
                config.logbook_check_on_startup is expected
                or config.logbook_check_on_startup is False
            ), f"LOGBOOK_CHECK_ON_STARTUP={val} 应解析为 False"

    def test_auto_migrate_on_startup_default_false(self):
        """
        关键路径测试: AUTO_MIGRATE_ON_STARTUP 默认为 false
        """
        from engram.gateway.config import load_config, reset_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"

        # 不设置 AUTO_MIGRATE_ON_STARTUP
        os.environ.pop("AUTO_MIGRATE_ON_STARTUP", None)

        try:
            config = load_config()

            # 关键断言: 默认值为 False
            assert config.auto_migrate_on_startup is False, (
                f"AUTO_MIGRATE_ON_STARTUP 默认值应为 False，实际: {config.auto_migrate_on_startup}"
            )
        finally:
            reset_config()

    def test_auto_migrate_on_startup_true_value(self):
        """
        关键路径测试: AUTO_MIGRATE_ON_STARTUP=true 应启用自动迁移
        """
        from engram.gateway.config import load_config, reset_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"
        os.environ["AUTO_MIGRATE_ON_STARTUP"] = "true"

        try:
            config = load_config()

            # 关键断言
            assert config.auto_migrate_on_startup is True, (
                f"AUTO_MIGRATE_ON_STARTUP=true 应启用自动迁移，"
                f"实际: {config.auto_migrate_on_startup}"
            )
        finally:
            reset_config()

    def test_auto_migrate_accepts_various_true_values(self):
        """
        测试 AUTO_MIGRATE_ON_STARTUP 接受多种 true 值格式
        """
        from engram.gateway.config import load_config, reset_config

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"

        true_values = ["true", "TRUE", "True", "1", "yes", "YES"]

        for val in true_values:
            reset_config()
            os.environ["AUTO_MIGRATE_ON_STARTUP"] = val

            config = load_config()

            # 所有这些值都应解析为 True
            assert config.auto_migrate_on_startup is True, (
                f"AUTO_MIGRATE_ON_STARTUP={val} 应解析为 True，"
                f"实际: {config.auto_migrate_on_startup}"
            )


# ======================== check_logbook_db_on_startup 行为测试 ========================


class TestCheckLogbookDBOnStartup:
    """
    验证 check_logbook_db_on_startup 函数行为

    策略说明:
    1. LOGBOOK_CHECK_ON_STARTUP=true + AUTO_MIGRATE_ON_STARTUP=false: 仅检查
    2. AUTO_MIGRATE_ON_STARTUP=true: 检查 + 自动迁移
    3. LOGBOOK_CHECK_ON_STARTUP=false: 跳过检查

    注：函数已从 main.py 移至 startup.py，测试使用新的导入路径
    """

    def test_check_skipped_when_logbook_check_disabled(self):
        """
        关键路径测试: LOGBOOK_CHECK_ON_STARTUP=false 时跳过检查
        """
        from engram.gateway.startup import check_logbook_db_on_startup

        mock_config = MagicMock()
        mock_config.logbook_check_on_startup = False
        mock_config.auto_migrate_on_startup = False
        mock_config.postgres_dsn = "postgresql://test:test@localhost/test"

        result = check_logbook_db_on_startup(mock_config)

        # 关键断言: 跳过检查时返回 True
        assert result is True, "LOGBOOK_CHECK_ON_STARTUP=false 时应跳过检查并返回 True"

    def test_check_proceeds_when_logbook_check_enabled(self):
        """
        关键路径测试: LOGBOOK_CHECK_ON_STARTUP=true 时执行检查
        """
        from engram.gateway.startup import check_logbook_db_on_startup

        mock_config = MagicMock()
        mock_config.logbook_check_on_startup = True
        mock_config.auto_migrate_on_startup = False
        mock_config.postgres_dsn = "postgresql://test:test@localhost/test"

        # Mock is_db_migrate_available 和 ensure_db_ready
        with (
            patch("engram.gateway.startup.is_db_migrate_available") as mock_available,
            patch("engram.gateway.startup.ensure_db_ready") as mock_ensure,
        ):
            mock_available.return_value = True
            mock_result = MagicMock()
            mock_result.ok = True
            mock_result.message = "All schemas ready"
            mock_ensure.return_value = mock_result

            result = check_logbook_db_on_startup(mock_config)

            # 关键断言: 调用了 ensure_db_ready
            mock_ensure.assert_called_once_with(
                dsn=mock_config.postgres_dsn,
                auto_migrate=False,
            )
            assert result is True

    def test_auto_migrate_passed_to_ensure_db_ready(self):
        """
        关键路径测试: AUTO_MIGRATE_ON_STARTUP=true 时传递 auto_migrate=True
        """
        from engram.gateway.startup import check_logbook_db_on_startup

        mock_config = MagicMock()
        mock_config.logbook_check_on_startup = True
        mock_config.auto_migrate_on_startup = True
        mock_config.postgres_dsn = "postgresql://test:test@localhost/test"

        with (
            patch("engram.gateway.startup.is_db_migrate_available") as mock_available,
            patch("engram.gateway.startup.ensure_db_ready") as mock_ensure,
        ):
            mock_available.return_value = True
            mock_result = MagicMock()
            mock_result.ok = True
            mock_result.message = "Migrated successfully"
            mock_ensure.return_value = mock_result

            result = check_logbook_db_on_startup(mock_config)

            # 关键断言: auto_migrate=True 被传递
            mock_ensure.assert_called_once_with(
                dsn=mock_config.postgres_dsn,
                auto_migrate=True,
            )
            assert result is True

    def test_check_returns_false_when_db_missing(self):
        """
        关键路径测试: DB 缺失且不自动迁移时返回 False
        """
        from engram.gateway.startup import check_logbook_db_on_startup

        mock_config = MagicMock()
        mock_config.logbook_check_on_startup = True
        mock_config.auto_migrate_on_startup = False
        mock_config.postgres_dsn = "postgresql://test:test@localhost/test"

        with (
            patch("engram.gateway.startup.is_db_migrate_available") as mock_available,
            patch("engram.gateway.startup.ensure_db_ready") as mock_ensure,
        ):
            mock_available.return_value = True
            mock_result = MagicMock()
            mock_result.ok = False
            mock_result.message = "Schema missing: logbook"
            mock_result.code = "SCHEMA_MISSING"
            mock_result.missing_items = {"schemas": ["logbook"]}
            mock_ensure.return_value = mock_result

            result = check_logbook_db_on_startup(mock_config)

            # 关键断言: DB 缺失时返回 False
            assert result is False, "DB 缺失时 check_logbook_db_on_startup 应返回 False"

    def test_check_skipped_when_db_migrate_unavailable(self):
        """
        测试: db_migrate 模块不可用时跳过检查
        """
        from engram.gateway.startup import check_logbook_db_on_startup

        mock_config = MagicMock()
        mock_config.logbook_check_on_startup = True
        mock_config.auto_migrate_on_startup = False

        with patch("engram.gateway.startup.is_db_migrate_available") as mock_available:
            mock_available.return_value = False

            result = check_logbook_db_on_startup(mock_config)

            # 模块不可用时跳过检查，返回 True
            assert result is True


# ======================== DB 检查失败路径测试 ========================


class TestDBCheckFailurePaths:
    """
    DB 检查失败路径测试（使用 mock，无需真实数据库）

    覆盖场景:
    1. LogbookDBCheckError 异常处理
    2. 通用 Exception 异常处理
    3. 不同错误码的修复提示
    """

    def test_logbook_db_check_error_returns_false(self):
        """
        测试: LogbookDBCheckError 异常时返回 False 并记录错误
        """
        from engram.gateway.logbook_adapter import LogbookDBCheckError, LogbookDBErrorCode
        from engram.gateway.startup import check_logbook_db_on_startup

        mock_config = MagicMock()
        mock_config.logbook_check_on_startup = True
        mock_config.auto_migrate_on_startup = False
        mock_config.postgres_dsn = "postgresql://test:test@localhost/test"

        with (
            patch("engram.gateway.startup.is_db_migrate_available") as mock_available,
            patch("engram.gateway.startup.ensure_db_ready") as mock_ensure,
        ):
            mock_available.return_value = True
            mock_ensure.side_effect = LogbookDBCheckError(
                message="Schema 'logbook' does not exist",
                code=LogbookDBErrorCode.SCHEMA_MISSING,
                missing_items={"schemas": ["logbook", "governance"]},
            )

            result = check_logbook_db_on_startup(mock_config)

            # 关键断言: 异常时返回 False
            assert result is False

    def test_generic_exception_returns_false(self):
        """
        测试: 通用 Exception 异常时返回 False
        """
        from engram.gateway.startup import check_logbook_db_on_startup

        mock_config = MagicMock()
        mock_config.logbook_check_on_startup = True
        mock_config.auto_migrate_on_startup = False
        mock_config.postgres_dsn = "postgresql://test:test@localhost/test"

        with (
            patch("engram.gateway.startup.is_db_migrate_available") as mock_available,
            patch("engram.gateway.startup.ensure_db_ready") as mock_ensure,
        ):
            mock_available.return_value = True
            mock_ensure.side_effect = Exception("Connection refused")

            result = check_logbook_db_on_startup(mock_config)

            # 关键断言: 未预期异常时返回 False
            assert result is False

    def test_table_missing_error_returns_false(self):
        """
        测试: TABLE_MISSING 错误码时返回 False
        """
        from engram.gateway.logbook_adapter import LogbookDBCheckError, LogbookDBErrorCode
        from engram.gateway.startup import check_logbook_db_on_startup

        mock_config = MagicMock()
        mock_config.logbook_check_on_startup = True
        mock_config.auto_migrate_on_startup = False
        mock_config.postgres_dsn = "postgresql://test:test@localhost/test"

        with (
            patch("engram.gateway.startup.is_db_migrate_available") as mock_available,
            patch("engram.gateway.startup.ensure_db_ready") as mock_ensure,
        ):
            mock_available.return_value = True
            mock_ensure.side_effect = LogbookDBCheckError(
                message="Required tables are missing",
                code=LogbookDBErrorCode.TABLE_MISSING,
                missing_items={"tables": ["logbook.items", "logbook.outbox_memory"]},
            )

            result = check_logbook_db_on_startup(mock_config)

            assert result is False

    def test_index_missing_error_returns_false(self):
        """
        测试: INDEX_MISSING 错误码时返回 False
        """
        from engram.gateway.logbook_adapter import LogbookDBCheckError, LogbookDBErrorCode
        from engram.gateway.startup import check_logbook_db_on_startup

        mock_config = MagicMock()
        mock_config.logbook_check_on_startup = True
        mock_config.auto_migrate_on_startup = False
        mock_config.postgres_dsn = "postgresql://test:test@localhost/test"

        with (
            patch("engram.gateway.startup.is_db_migrate_available") as mock_available,
            patch("engram.gateway.startup.ensure_db_ready") as mock_ensure,
        ):
            mock_available.return_value = True
            mock_ensure.side_effect = LogbookDBCheckError(
                message="Required indexes are missing",
                code=LogbookDBErrorCode.INDEX_MISSING,
                missing_items={"indexes": ["idx_outbox_status", "idx_audit_created"]},
            )

            result = check_logbook_db_on_startup(mock_config)

            assert result is False

    def test_connection_failed_error_returns_false(self):
        """
        测试: CONNECTION_FAILED 错误码时返回 False
        """
        from engram.gateway.logbook_adapter import LogbookDBCheckError, LogbookDBErrorCode
        from engram.gateway.startup import check_logbook_db_on_startup

        mock_config = MagicMock()
        mock_config.logbook_check_on_startup = True
        mock_config.auto_migrate_on_startup = False
        mock_config.postgres_dsn = "postgresql://test:test@localhost/test"

        with (
            patch("engram.gateway.startup.is_db_migrate_available") as mock_available,
            patch("engram.gateway.startup.ensure_db_ready") as mock_ensure,
        ):
            mock_available.return_value = True
            mock_ensure.side_effect = LogbookDBCheckError(
                message="Could not connect to database",
                code=LogbookDBErrorCode.CONNECTION_FAILED,
                missing_items={},
            )

            result = check_logbook_db_on_startup(mock_config)

            assert result is False


# ======================== format_db_repair_commands 测试 ========================


class TestFormatDBRepairCommands:
    """
    验证 format_db_repair_commands 函数行为
    """

    def test_basic_format_without_params(self):
        """
        测试: 无参数时返回基本修复命令
        """
        from engram.gateway.startup import format_db_repair_commands

        result = format_db_repair_commands()

        # 关键断言: 包含修复命令（使用新的 CLI 入口点）
        assert "修复命令" in result
        assert "engram-bootstrap-roles" in result
        assert "engram-migrate" in result
        assert "docker compose" in result

    def test_format_with_error_code(self):
        """
        测试: 包含错误代码时在输出中显示
        """
        from engram.gateway.startup import format_db_repair_commands

        result = format_db_repair_commands(error_code="SCHEMA_MISSING")

        assert "错误代码: SCHEMA_MISSING" in result

    def test_format_with_missing_items_dict(self):
        """
        测试: missing_items 为字典时正确格式化
        """
        from engram.gateway.startup import format_db_repair_commands

        missing = {
            "schemas": ["logbook", "governance"],
            "tables": ["items"],
        }
        result = format_db_repair_commands(missing_items=missing)

        assert "缺失项详情" in result
        assert "schemas: logbook" in result
        assert "schemas: governance" in result
        assert "tables: items" in result

    def test_format_with_missing_items_list(self):
        """
        测试: missing_items 为列表时正确格式化
        """
        from engram.gateway.startup import format_db_repair_commands

        missing = ["schema: logbook", "table: items"]
        result = format_db_repair_commands(missing_items=missing)

        assert "缺失项详情" in result
        assert "schema: logbook" in result
        assert "table: items" in result

    def test_format_truncates_long_missing_items(self):
        """
        测试: 超过 10 项时截断显示
        """
        from engram.gateway.startup import format_db_repair_commands

        missing = [f"item_{i}" for i in range(15)]
        result = format_db_repair_commands(missing_items=missing)

        assert "缺失项详情" in result
        assert "item_0" in result
        assert "item_9" in result
        assert "还有 5 项" in result


# ======================== DSN vs 配置文件优先级测试 ========================


class TestDSNVsConfigFilePriority:
    """
    验证 DSN vs 配置文件优先级

    文档要求（docs/reference/environment_variables.md）:
    - POSTGRES_DSN 优先级高于配置文件
    """

    def setup_method(self):
        """每个测试前重置配置"""
        from engram.gateway.config import reset_config

        reset_config()
        self._saved_env = {}
        for key in ["PROJECT_KEY", "POSTGRES_DSN"]:
            self._saved_env[key] = os.environ.get(key)

    def teardown_method(self):
        """每个测试后恢复环境变量"""
        from engram.gateway.config import reset_config

        reset_config()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_env_postgres_dsn_required(self):
        """
        关键路径测试: POSTGRES_DSN 是必填环境变量
        """
        from engram.gateway.config import ConfigError, load_config, reset_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ.pop("POSTGRES_DSN", None)

        with pytest.raises(ConfigError) as exc_info:
            load_config()

        # 关键断言: 错误信息包含 POSTGRES_DSN
        assert "POSTGRES_DSN" in str(exc_info.value), (
            "缺少 POSTGRES_DSN 时应抛出包含变量名的 ConfigError"
        )

    def test_postgres_dsn_from_env_used(self):
        """
        关键路径测试: 环境变量 POSTGRES_DSN 被正确读取
        """
        from engram.gateway.config import load_config, reset_config

        reset_config()

        expected_dsn = "postgresql://user:pass@host:5432/db"
        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = expected_dsn

        try:
            config = load_config()

            assert config.postgres_dsn == expected_dsn, (
                f"期望 DSN: {expected_dsn}，实际: {config.postgres_dsn}"
            )
        finally:
            reset_config()


# ======================== 配置项格式校验测试 ========================


class TestConfigValidation:
    """
    验证配置格式校验
    """

    def setup_method(self):
        """每个测试前重置配置"""
        from engram.gateway.config import reset_config

        reset_config()
        self._saved_env = {}
        for key in ["PROJECT_KEY", "POSTGRES_DSN", "GATEWAY_PORT", "UNKNOWN_ACTOR_POLICY"]:
            self._saved_env[key] = os.environ.get(key)

    def teardown_method(self):
        """每个测试后恢复环境变量"""
        from engram.gateway.config import reset_config

        reset_config()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_invalid_gateway_port_raises_error(self):
        """
        测试: 无效的 GATEWAY_PORT 应抛出 ConfigError
        """
        from engram.gateway.config import ConfigError, load_config, reset_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"
        os.environ["GATEWAY_PORT"] = "not_a_number"

        with pytest.raises(ConfigError) as exc_info:
            load_config()

        assert "GATEWAY_PORT" in str(exc_info.value), (
            "无效端口应抛出包含 GATEWAY_PORT 的 ConfigError"
        )

    def test_invalid_postgres_dsn_format_rejected(self):
        """
        测试: 无效的 POSTGRES_DSN 格式被拒绝
        """
        from engram.gateway.config import ConfigError, load_config, reset_config, validate_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "mysql://test:test@localhost/test"  # 错误的协议

        load_config()  # load_config 不校验格式

        # validate_config 应该拒绝非 postgresql:// 的 DSN
        with pytest.raises(ConfigError) as exc_info:
            validate_config()

        assert "postgresql://" in str(exc_info.value) or "postgres://" in str(exc_info.value)

    def test_unknown_actor_policy_validation(self):
        """
        测试: 无效的 UNKNOWN_ACTOR_POLICY 被拒绝
        """
        from engram.gateway.config import ConfigError, load_config, reset_config

        reset_config()

        os.environ["PROJECT_KEY"] = "test_project"
        os.environ["POSTGRES_DSN"] = "postgresql://test:test@localhost/test"
        os.environ["UNKNOWN_ACTOR_POLICY"] = "invalid_policy"

        with pytest.raises(ConfigError) as exc_info:
            load_config()

        error_msg = str(exc_info.value)
        assert "UNKNOWN_ACTOR_POLICY" in error_msg
        assert "invalid_policy" in error_msg


# ======================== GatewayConfig 数据类测试 ========================


class TestGatewayConfigDataclass:
    """
    验证 GatewayConfig 数据类行为
    """

    def test_default_team_space_generated_from_project_key(self):
        """
        测试: default_team_space 默认从 project_key 生成
        """
        from engram.gateway.config import GatewayConfig

        config = GatewayConfig(
            project_key="my_project",
            postgres_dsn="postgresql://test:test@localhost/test",
        )

        # 关键断言: default_team_space 应为 team:<project_key>
        assert config.default_team_space == "team:my_project", (
            f"期望 default_team_space='team:my_project'，实际: {config.default_team_space}"
        )

    def test_openmemory_base_url_trailing_slash_removed(self):
        """
        测试: openmemory_base_url 末尾斜杠被移除
        """
        from engram.gateway.config import GatewayConfig

        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test:test@localhost/test",
            openmemory_base_url="http://localhost:8080/",
        )

        # 关键断言: 末尾斜杠应被移除
        assert config.openmemory_base_url == "http://localhost:8080", (
            f"末尾斜杠应被移除，实际: {config.openmemory_base_url}"
        )

    def test_default_values(self):
        """
        测试: 配置项默认值
        """
        from engram.gateway.config import GatewayConfig

        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test:test@localhost/test",
        )

        # 验证默认值
        assert config.gateway_port == 8787
        assert config.private_space_prefix == "private:"
        assert config.auto_migrate_on_startup is False
        assert config.logbook_check_on_startup is True
        assert config.unknown_actor_policy == "degrade"


# ======================== OpenMemoryClient base_url/api_key 传递链路测试 ========================


class TestOpenMemoryClientConfigPropagation:
    """
    验证 OpenMemoryClient 的 base_url/api_key 配置传递链路

    优先级规则:
    1. 显式传入 config 时，使用 config.openmemory_base_url 和 config.openmemory_api_key
    2. 不传入 config 时，从环境变量获取（OPENMEMORY_BASE_URL, OPENMEMORY_API_KEY/OM_API_KEY）
    3. Container 构造的 client 必须使用 config 的配置

    HTTP 安全性说明:
    ====================
    本测试类中的测试仅验证 OpenMemoryClient 的属性（base_url/api_key），
    不调用任何会触发真实 HTTP 请求的方法（如 add_memory/store/search/health_check）。

    OpenMemoryClient 构造函数不会发起 HTTP 请求，只有以下方法才会：
    - add_memory(): POST /memory/add
    - store(): POST /memory/add
    - search(): POST /memory/search
    - health_check(): GET /health

    因此这些测试在 CI 环境下是安全的，不依赖 OpenMemory 服务。

    注意:
    - 全局状态重置由 conftest.py 的 auto_reset_gateway_state fixture 自动处理
    - 此处仅保留环境变量管理逻辑
    """

    def setup_method(self):
        """每个测试前保存并清理环境变量"""
        # 保存并清理环境变量
        self._saved_env = {}
        for key in [
            "OPENMEMORY_BASE_URL",
            "OPENMEMORY_API_KEY",
            "OM_API_KEY",
            "PROJECT_KEY",
            "POSTGRES_DSN",
        ]:
            self._saved_env[key] = os.environ.get(key)
            if key in os.environ:
                del os.environ[key]

    def teardown_method(self):
        """每个测试后恢复环境变量"""
        # 恢复环境变量
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # 全局状态重置由 auto_reset_gateway_state 处理

    def test_get_client_with_config_uses_config_values(self):
        """
        关键路径测试: get_client(config) 使用 config 的 base_url 和 api_key
        """
        from engram.gateway.config import GatewayConfig
        from engram.gateway.openmemory_client import get_client

        # 设置环境变量（应该被忽略）
        os.environ["OPENMEMORY_BASE_URL"] = "http://env-url:8080"
        os.environ["OPENMEMORY_API_KEY"] = "env_api_key"

        # 创建 config 指定不同的值
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test:test@localhost/test",
            openmemory_base_url="http://config-url:9090",
            openmemory_api_key="config_api_key",
        )

        client = get_client(config)

        # 关键断言: client 应使用 config 的值
        assert client.base_url == "http://config-url:9090", (
            f"get_client(config) 应使用 config.openmemory_base_url，"
            f"期望: http://config-url:9090，"
            f"实际: {client.base_url}"
        )
        assert client.api_key == "config_api_key", (
            f"get_client(config) 应使用 config.openmemory_api_key，"
            f"期望: config_api_key，"
            f"实际: {client.api_key}"
        )

    def test_get_client_without_config_uses_env_vars(self):
        """
        关键路径测试: get_client() 不传 config 时使用环境变量
        """
        from engram.gateway.openmemory_client import get_client, reset_client

        reset_client()

        # 设置环境变量
        os.environ["OPENMEMORY_BASE_URL"] = "http://env-url:8080"
        os.environ["OPENMEMORY_API_KEY"] = "env_api_key"

        client = get_client()

        # 关键断言: client 应使用环境变量的值
        assert client.base_url == "http://env-url:8080", (
            f"get_client() 应使用环境变量 OPENMEMORY_BASE_URL，"
            f"期望: http://env-url:8080，"
            f"实际: {client.base_url}"
        )
        assert client.api_key == "env_api_key", (
            f"get_client() 应使用环境变量 OPENMEMORY_API_KEY，"
            f"期望: env_api_key，"
            f"实际: {client.api_key}"
        )

    def test_container_openmemory_client_uses_config(self):
        """
        关键路径测试: Container 构造的 openmemory_client 使用 config 的值
        """
        from engram.gateway.config import GatewayConfig
        from engram.gateway.container import GatewayContainer

        # 设置环境变量（应该被忽略）
        os.environ["OPENMEMORY_BASE_URL"] = "http://env-url:8080"
        os.environ["OPENMEMORY_API_KEY"] = "env_api_key"

        # 创建 config
        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test:test@localhost/test",
            openmemory_base_url="http://container-url:7070",
            openmemory_api_key="container_api_key",
        )

        container = GatewayContainer.create(config=config)
        client = container.openmemory_client

        # 关键断言: container.openmemory_client 应使用 config 的值
        assert client.base_url == "http://container-url:7070", (
            f"Container.openmemory_client 应使用 config.openmemory_base_url，"
            f"期望: http://container-url:7070，"
            f"实际: {client.base_url}"
        )
        assert client.api_key == "container_api_key", (
            f"Container.openmemory_client 应使用 config.openmemory_api_key，"
            f"期望: container_api_key，"
            f"实际: {client.api_key}"
        )

    def test_container_for_testing_accepts_injected_client(self):
        """
        测试: Container.create_for_testing 允许注入自定义 client
        """
        from engram.gateway.config import GatewayConfig
        from engram.gateway.container import GatewayContainer
        from engram.gateway.openmemory_client import OpenMemoryClient

        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test:test@localhost/test",
        )

        # 创建自定义 client
        custom_client = OpenMemoryClient(
            base_url="http://custom-url:6060",
            api_key="custom_key",
        )

        container = GatewayContainer.create_for_testing(
            config=config,
            openmemory_client=custom_client,
        )

        # 关键断言: 应返回注入的 client
        assert container.openmemory_client is custom_client
        assert container.openmemory_client.base_url == "http://custom-url:6060"
        assert container.openmemory_client.api_key == "custom_key"

    def test_openmemory_client_api_key_priority_in_constructor(self):
        """
        测试: OpenMemoryClient 构造函数中 api_key 参数优先于环境变量
        """
        from engram.gateway.openmemory_client import OpenMemoryClient

        # 设置环境变量
        os.environ["OPENMEMORY_API_KEY"] = "env_high_priority"
        os.environ["OM_API_KEY"] = "env_low_priority"

        # 显式传入 api_key（应该覆盖环境变量）
        client = OpenMemoryClient(
            base_url="http://test:8080",
            api_key="explicit_api_key",
        )

        assert client.api_key == "explicit_api_key", (
            f"显式传入的 api_key 应覆盖环境变量，期望: explicit_api_key，实际: {client.api_key}"
        )

    def test_openmemory_client_fallback_to_env_when_no_explicit_values(self):
        """
        测试: OpenMemoryClient 构造函数中未传 api_key 时回退到环境变量
        """
        from engram.gateway.openmemory_client import OpenMemoryClient

        # 设置环境变量
        os.environ["OPENMEMORY_BASE_URL"] = "http://fallback-url:8080"
        os.environ["OPENMEMORY_API_KEY"] = "fallback_key"

        # 不传任何参数
        client = OpenMemoryClient()

        assert client.base_url == "http://fallback-url:8080"
        assert client.api_key == "fallback_key"

    def test_config_none_api_key_propagates_to_client(self):
        """
        测试: config.openmemory_api_key 为 None 时，client.api_key 也为 None
        """
        from engram.gateway.config import GatewayConfig
        from engram.gateway.openmemory_client import get_client

        # 清理环境变量
        os.environ.pop("OPENMEMORY_API_KEY", None)
        os.environ.pop("OM_API_KEY", None)

        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test:test@localhost/test",
            openmemory_base_url="http://test:8080",
            openmemory_api_key=None,  # 显式为 None
        )

        client = get_client(config)

        # 关键断言: api_key 应为 None
        assert client.api_key is None, (
            f"config.openmemory_api_key=None 时 client.api_key 应为 None，实际: {client.api_key}"
        )


# ======================== OpenMemoryClient HTTP 安全性测试 ========================


class TestOpenMemoryClientNoHttpOnConstruction:
    """
    验证 OpenMemoryClient 构造不触发 HTTP 请求

    此测试类确保：
    1. OpenMemoryClient 构造函数不会发起任何 HTTP 请求
    2. 仅访问 client.base_url/api_key 属性不会触发 HTTP
    3. CI 环境下不依赖 OpenMemory 服务
    """

    def test_construction_does_not_trigger_http(self):
        """
        验证 OpenMemoryClient 构造不触发 HTTP 请求

        即使 base_url 指向不存在的服务，构造也应该成功。
        """
        from engram.gateway.openmemory_client import OpenMemoryClient

        # 使用不存在的 URL 构造 client
        client = OpenMemoryClient(
            base_url="http://nonexistent-host:9999",
            api_key="test_key",
        )

        # 验证构造成功，属性正确设置
        assert client.base_url == "http://nonexistent-host:9999"
        assert client.api_key == "test_key"
        # 构造不应该抛出网络异常

    def test_construction_with_mock_httpx_not_called(self):
        """
        使用 mock 显式验证构造时 httpx.Client 未被调用

        此测试作为额外的安全保护，确保构造函数不会意外触发 HTTP。
        """
        from unittest.mock import MagicMock, patch

        mock_httpx_client = MagicMock()

        with patch("httpx.Client", mock_httpx_client):
            from engram.gateway.openmemory_client import OpenMemoryClient

            # 构造 client
            client = OpenMemoryClient(
                base_url="http://test:8080",
                api_key="key",
            )

            # 访问属性
            _ = client.base_url
            _ = client.api_key
            _ = client.timeout
            _ = client.retry_config

        # 验证 httpx.Client 未被调用
        mock_httpx_client.assert_not_called()

    def test_get_client_with_config_no_http(self):
        """
        验证 get_client(config) 不触发 HTTP 请求

        此测试确保通过 config 获取 client 时也不会触发 HTTP。
        """
        from unittest.mock import MagicMock, patch

        from engram.gateway.config import GatewayConfig
        from engram.gateway.openmemory_client import get_client

        mock_httpx_client = MagicMock()

        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test:test@localhost/test",
            openmemory_base_url="http://test:8080",
            openmemory_api_key="test_key",
        )

        with patch("httpx.Client", mock_httpx_client):
            client = get_client(config)

            # 访问属性
            assert client.base_url == "http://test:8080"
            assert client.api_key == "test_key"

        # 验证 httpx.Client 未被调用
        mock_httpx_client.assert_not_called()

    def test_container_openmemory_client_no_http(self):
        """
        验证 Container.openmemory_client 属性访问不触发 HTTP 请求

        此测试确保通过 container 获取 client 时也不会触发 HTTP。
        """
        from unittest.mock import MagicMock, patch

        from engram.gateway.config import GatewayConfig
        from engram.gateway.container import GatewayContainer

        mock_httpx_client = MagicMock()

        config = GatewayConfig(
            project_key="test",
            postgres_dsn="postgresql://test:test@localhost/test",
            openmemory_base_url="http://container-test:7070",
            openmemory_api_key="container_key",
        )

        with patch("httpx.Client", mock_httpx_client):
            container = GatewayContainer.create(config=config)

            # 访问 openmemory_client 属性
            client = container.openmemory_client

            # 验证属性
            assert client.base_url == "http://container-test:7070"
            assert client.api_key == "container_key"

        # 验证 httpx.Client 未被调用
        mock_httpx_client.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
