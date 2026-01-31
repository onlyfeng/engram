# -*- coding: utf-8 -*-
"""
安装验证测试

验证 pip 安装后各组件可正常使用:
- 核心模块导入
- Gateway 模块导入
- 版本号可获取
- 关键类和函数可用
"""

import pytest


class TestCoreImport:
    """核心模块导入测试"""

    def test_engram_package_import(self):
        """engram 包可导入"""
        import engram

        assert engram is not None

    def test_engram_version(self):
        """版本号可获取"""
        import engram

        assert hasattr(engram, "__version__")
        assert isinstance(engram.__version__, str)
        assert len(engram.__version__) > 0

    def test_logbook_module_import(self):
        """engram.logbook 模块可导入"""
        from engram import logbook

        assert logbook is not None

    def test_logbook_database_class(self):
        """Database 类可导入"""
        from engram.logbook import Database

        assert Database is not None
        assert callable(Database)

    def test_logbook_config_class(self):
        """Config 类可导入"""
        from engram.logbook import Config

        assert Config is not None
        assert callable(Config)

    def test_logbook_errors(self):
        """错误类可导入"""
        from engram.logbook.errors import (
            ConfigError,
            DatabaseError,
            EngramError,
            ValidationError,
        )

        # 验证是异常类
        assert issubclass(EngramError, Exception)
        assert issubclass(ConfigError, EngramError)
        assert issubclass(DatabaseError, EngramError)
        assert issubclass(ValidationError, EngramError)

    def test_logbook_hashing_functions(self):
        """哈希函数可导入和使用"""
        from engram.logbook import (
            hash_bytes,
            hash_string,
            md5,
            sha1,
            sha256,
        )

        # 验证函数可调用
        assert callable(hash_bytes)
        assert callable(hash_string)
        assert callable(sha256)
        assert callable(sha1)
        assert callable(md5)

        # 验证基本功能
        test_data = b"hello world"
        result = sha256(test_data)
        assert isinstance(result, str)
        assert len(result) == 64  # SHA256 hex digest length

    def test_logbook_io_functions(self):
        """I/O 函数可导入"""
        from engram.logbook import (
            cli_wrapper,
            output_error,
            output_json,
            output_success,
        )

        assert callable(output_json)
        assert callable(output_success)
        assert callable(output_error)
        assert callable(cli_wrapper)

    def test_logbook_uri_functions(self):
        """URI 函数可导入"""
        from engram.logbook import (
            UriType,
            classify_uri,
            normalize_uri,
            parse_uri,
        )

        assert callable(parse_uri)
        assert callable(normalize_uri)
        assert callable(classify_uri)
        assert UriType is not None


class TestGatewayImport:
    """Gateway 模块导入测试"""

    def test_gateway_module_import(self):
        """engram.gateway 模块可导入"""
        try:
            from engram import gateway

            assert gateway is not None
        except ImportError as e:
            # Gateway 依赖可能未安装
            if "fastapi" in str(e).lower() or "uvicorn" in str(e).lower():
                pytest.skip("Gateway 依赖未安装 (fastapi/uvicorn)")
            raise

    def test_gateway_policy_engine(self):
        """PolicyEngine 类可导入"""
        try:
            from engram.gateway.policy import PolicyAction, PolicyDecision, PolicyEngine

            assert PolicyEngine is not None
            assert PolicyAction is not None
            assert PolicyDecision is not None
        except ImportError as e:
            if "fastapi" in str(e).lower():
                pytest.skip("Gateway 依赖未安装")
            raise

    def test_gateway_audit_event(self):
        """AuditEvent 类可导入"""
        try:
            from engram.gateway.audit_event import AuditEvent

            assert AuditEvent is not None
        except ImportError as e:
            if "fastapi" in str(e).lower() or "pydantic" in str(e).lower():
                pytest.skip("Gateway 依赖未安装")
            raise

    def test_gateway_mcp_rpc(self):
        """MCP RPC 模块可导入"""
        try:
            from engram.gateway import mcp_rpc

            assert mcp_rpc is not None
        except ImportError as e:
            if "fastapi" in str(e).lower():
                pytest.skip("Gateway 依赖未安装")
            raise


class TestOptionalDependencies:
    """可选依赖测试"""

    def test_scm_dependencies(self):
        """SCM 依赖可选导入"""
        # requests 是 SCM 依赖
        try:
            import requests

            assert requests is not None
        except ImportError:
            pytest.skip("SCM 依赖 (requests) 未安装")

    def test_gateway_dependencies(self):
        """Gateway 依赖可选导入"""
        missing = []

        try:
            import fastapi  # noqa: F401
        except ImportError:
            missing.append("fastapi")

        try:
            import uvicorn  # noqa: F401
        except ImportError:
            missing.append("uvicorn")

        try:
            import httpx  # noqa: F401
        except ImportError:
            missing.append("httpx")

        try:
            import pydantic  # noqa: F401
        except ImportError:
            missing.append("pydantic")

        if missing:
            pytest.skip(f"Gateway 依赖未安装: {', '.join(missing)}")


class TestDatabaseDependencies:
    """数据库依赖测试"""

    def test_psycopg_import(self):
        """psycopg 可导入"""
        import psycopg

        assert psycopg is not None

    def test_psycopg_binary(self):
        """psycopg binary 扩展可用"""
        try:
            import psycopg_binary

            assert psycopg_binary is not None
        except ImportError:
            # binary 扩展是可选的
            pass


class TestPackageMetadata:
    """包元数据测试"""

    def test_package_has_author(self):
        """包有作者信息"""
        from engram.logbook import __author__

        assert __author__ is not None
        assert isinstance(__author__, str)

    def test_package_version_format(self):
        """版本号格式正确"""
        import engram

        version = engram.__version__

        # 应该是 semver 格式: major.minor.patch
        parts = version.split(".")
        assert len(parts) >= 2, f"版本号格式不正确: {version}"

        # 主版本号和次版本号应该是数字
        assert parts[0].isdigit(), f"主版本号不是数字: {parts[0]}"
        assert parts[1].isdigit(), f"次版本号不是数字: {parts[1]}"


class TestLogbookModuleStructure:
    """Logbook 模块结构测试"""

    def test_all_exports_defined(self):
        """__all__ 定义完整"""
        from engram.logbook import __all__

        assert isinstance(__all__, list)
        assert len(__all__) > 0

    def test_all_exports_importable(self):
        """__all__ 中的所有导出项可导入"""
        from engram import logbook
        from engram.logbook import __all__

        for name in __all__:
            assert hasattr(logbook, name), f"导出项 {name} 不存在"

    def test_config_module_structure(self):
        """config 模块结构正确"""
        from engram.logbook.config import Config

        # Config 类应有必要的方法
        assert hasattr(Config, "from_env")
        assert hasattr(Config, "from_file") or hasattr(Config, "from_dict")

    def test_db_module_structure(self):
        """db 模块结构正确"""
        from engram.logbook.db import Database

        # Database 类存在
        assert Database is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
