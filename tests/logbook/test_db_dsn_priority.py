# -*- coding: utf-8 -*-
"""
测试 get_dsn 函数的 DSN 来源优先级

优先级（高到低）：
1. config 中的 postgres.dsn 显式配置
2. 环境变量 POSTGRES_DSN
3. 环境变量 TEST_PG_DSN
"""

import os
import pytest
from unittest.mock import MagicMock, patch

from engram.logbook.db import get_dsn
from engram.logbook.errors import ConfigError


class TestGetDsnPriority:
    """测试 get_dsn 的 DSN 来源优先级"""

    def test_config_dsn_has_highest_priority(self):
        """
        显式配置优先于环境变量
        
        当 config 中有 postgres.dsn 时，即使环境变量也设置了，
        应该优先使用 config 中的值。
        """
        config_dsn = "postgresql://config:pass@config-host:5432/config_db"
        env_dsn = "postgresql://env:pass@env-host:5432/env_db"
        test_dsn = "postgresql://test:pass@test-host:5432/test_db"

        # Mock config
        mock_config = MagicMock()
        mock_config.get.return_value = config_dsn

        # 同时设置环境变量
        with patch.dict(os.environ, {
            "POSTGRES_DSN": env_dsn,
            "TEST_PG_DSN": test_dsn,
        }):
            result = get_dsn(config=mock_config)
            assert result == config_dsn, "显式配置应该优先于环境变量"

    def test_postgres_dsn_env_over_test_pg_dsn(self):
        """
        POSTGRES_DSN 环境变量优先于 TEST_PG_DSN
        """
        env_dsn = "postgresql://env:pass@env-host:5432/env_db"
        test_dsn = "postgresql://test:pass@test-host:5432/test_db"

        # Mock config 返回空
        mock_config = MagicMock()
        mock_config.get.return_value = None

        with patch.dict(os.environ, {
            "POSTGRES_DSN": env_dsn,
            "TEST_PG_DSN": test_dsn,
        }):
            result = get_dsn(config=mock_config)
            assert result == env_dsn, "POSTGRES_DSN 应该优先于 TEST_PG_DSN"

    def test_test_pg_dsn_fallback(self):
        """
        当 config 和 POSTGRES_DSN 都不存在时，回退到 TEST_PG_DSN
        """
        test_dsn = "postgresql://test:pass@test-host:5432/test_db"

        # Mock config 返回空
        mock_config = MagicMock()
        mock_config.get.return_value = None

        # 确保 POSTGRES_DSN 不存在
        env = os.environ.copy()
        env.pop("POSTGRES_DSN", None)
        env["TEST_PG_DSN"] = test_dsn

        with patch.dict(os.environ, env, clear=True):
            result = get_dsn(config=mock_config)
            assert result == test_dsn, "应该回退到 TEST_PG_DSN"

    def test_raise_error_when_no_dsn_available(self):
        """
        当所有来源都没有 DSN 时，抛出 ConfigError
        """
        # Mock config 返回空
        mock_config = MagicMock()
        mock_config.get.return_value = None

        # 清除所有相关环境变量
        env = os.environ.copy()
        env.pop("POSTGRES_DSN", None)
        env.pop("TEST_PG_DSN", None)

        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ConfigError) as exc_info:
                get_dsn(config=mock_config)
            
            # 验证错误信息包含检查过的来源
            assert "postgres.dsn" in str(exc_info.value.details)
            assert "POSTGRES_DSN" in str(exc_info.value.details)
            assert "TEST_PG_DSN" in str(exc_info.value.details)

    def test_config_empty_string_falls_back_to_env(self):
        """
        当 config 返回空字符串时，应该回退到环境变量
        """
        env_dsn = "postgresql://env:pass@env-host:5432/env_db"

        # Mock config 返回空字符串
        mock_config = MagicMock()
        mock_config.get.return_value = ""

        with patch.dict(os.environ, {"POSTGRES_DSN": env_dsn}):
            result = get_dsn(config=mock_config)
            assert result == env_dsn, "空字符串配置应该回退到环境变量"
