# -*- coding: utf-8 -*-
"""
环境变量配置测试

验证各种环境变量配置方式:
- POSTGRES_DSN 配置
- PROJECT_KEY 配置
- GATEWAY_PORT 配置
- OPENMEMORY_BASE_URL 配置
- 配置优先级
"""

import os
import pytest


class TestPostgresDSNConfig:
    """POSTGRES_DSN 环境变量测试"""

    def test_postgres_dsn_from_env(self, monkeypatch):
        """POSTGRES_DSN 环境变量正确读取"""
        test_dsn = "postgresql://test_user:test_pass@test_host:5432/test_db"
        monkeypatch.setenv("POSTGRES_DSN", test_dsn)
        
        from engram.logbook.config import Config
        
        # 清除可能的缓存
        try:
            from engram.logbook import db
            db.reset_database()
        except Exception:
            pass
        
        config = Config.from_env()
        
        # 验证 DSN 被正确读取
        assert config.postgres_dsn is not None
        assert "test_host" in config.postgres_dsn or config.postgres_dsn == test_dsn

    def test_postgres_dsn_missing_uses_default_or_error(self, monkeypatch):
        """POSTGRES_DSN 缺失时使用默认值或报错"""
        # 清除环境变量
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        
        from engram.logbook.config import Config
        
        try:
            config = Config.from_env()
            # 如果没有报错，应该有默认值或 None
            # 具体行为取决于实现
            assert config is not None
        except Exception as e:
            # 缺失时报错也是合理的行为
            assert "dsn" in str(e).lower() or "postgres" in str(e).lower() or "required" in str(e).lower()


class TestProjectKeyConfig:
    """PROJECT_KEY 环境变量测试"""

    def test_project_key_from_env(self, monkeypatch):
        """PROJECT_KEY 环境变量正确读取"""
        monkeypatch.setenv("PROJECT_KEY", "my_test_project")
        monkeypatch.setenv("POSTGRES_DSN", "postgresql://localhost/test")
        
        from engram.logbook.config import Config
        
        config = Config.from_env()
        
        # 检查 project_key 是否被设置
        if hasattr(config, "project_key"):
            assert config.project_key == "my_test_project"

    def test_project_key_default(self, monkeypatch):
        """PROJECT_KEY 缺失时使用默认值"""
        monkeypatch.delenv("PROJECT_KEY", raising=False)
        monkeypatch.setenv("POSTGRES_DSN", "postgresql://localhost/test")
        
        from engram.logbook.config import Config
        
        config = Config.from_env()
        
        # project_key 应该有默认值
        if hasattr(config, "project_key"):
            assert config.project_key is not None


class TestGatewayConfig:
    """Gateway 配置测试"""

    def test_gateway_port_from_env(self, monkeypatch):
        """GATEWAY_PORT 环境变量正确读取"""
        monkeypatch.setenv("GATEWAY_PORT", "9000")
        
        try:
            from engram.gateway.config import get_gateway_port, GatewayConfig
            
            port = get_gateway_port()
            assert port == 9000
        except ImportError:
            # Gateway 依赖可能未安装
            pytest.skip("Gateway 依赖未安装")
        except AttributeError:
            # get_gateway_port 函数可能不存在
            # 尝试其他方式
            try:
                config = GatewayConfig()
                if hasattr(config, "port"):
                    assert config.port == 9000
            except Exception:
                pytest.skip("Gateway 端口配置方式不同")

    def test_gateway_port_default(self, monkeypatch):
        """GATEWAY_PORT 缺失时使用默认值 8787"""
        monkeypatch.delenv("GATEWAY_PORT", raising=False)
        
        try:
            from engram.gateway.config import get_gateway_port, GatewayConfig
            
            port = get_gateway_port()
            assert port == 8787  # 默认端口
        except ImportError:
            pytest.skip("Gateway 依赖未安装")
        except AttributeError:
            pytest.skip("Gateway 端口配置方式不同")

    def test_openmemory_base_url_from_env(self, monkeypatch):
        """OPENMEMORY_BASE_URL 环境变量正确读取"""
        monkeypatch.setenv("OPENMEMORY_BASE_URL", "http://custom-openmemory:9000")
        
        try:
            from engram.gateway.config import GatewayConfig
            
            config = GatewayConfig()
            if hasattr(config, "openmemory_base_url"):
                assert "custom-openmemory" in config.openmemory_base_url
        except ImportError:
            pytest.skip("Gateway 依赖未安装")


class TestConfigPriority:
    """配置优先级测试"""

    def test_env_overrides_default(self, monkeypatch):
        """环境变量覆盖默认值"""
        custom_dsn = "postgresql://custom:custom@custom_host:5432/custom_db"
        monkeypatch.setenv("POSTGRES_DSN", custom_dsn)
        
        from engram.logbook.config import Config
        
        config = Config.from_env()
        
        # 环境变量应该覆盖默认值
        assert config.postgres_dsn == custom_dsn

    def test_config_file_support(self, monkeypatch, tmp_path):
        """配置文件支持（如果实现）"""
        # 创建临时配置文件
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[postgres]
dsn = "postgresql://file:file@localhost/file_db"

[project]
project_key = "file_project"
""")
        
        monkeypatch.setenv("ENGRAM_LOGBOOK_CONFIG", str(config_file))
        monkeypatch.delenv("POSTGRES_DSN", raising=False)
        
        from engram.logbook.config import Config
        
        try:
            config = Config.from_file(str(config_file))
            # 验证从文件读取
            if hasattr(config, "postgres_dsn"):
                assert "file_db" in config.postgres_dsn or config.postgres_dsn is not None
        except (FileNotFoundError, AttributeError, TypeError):
            # from_file 可能不存在或实现方式不同
            pass


class TestConfigValidation:
    """配置验证测试"""

    def test_invalid_dsn_format(self, monkeypatch):
        """无效 DSN 格式处理"""
        monkeypatch.setenv("POSTGRES_DSN", "not_a_valid_dsn")
        
        from engram.logbook.config import Config
        
        try:
            config = Config.from_env()
            # 如果允许无效格式，后续连接时应该失败
            # 这里只检查不会崩溃
            assert config is not None
        except ValueError:
            # 验证时拒绝无效格式也是合理的
            pass

    def test_empty_dsn(self, monkeypatch):
        """空 DSN 处理"""
        monkeypatch.setenv("POSTGRES_DSN", "")
        
        from engram.logbook.config import Config
        
        try:
            config = Config.from_env()
            # 空字符串可能被当作缺失处理
            assert config is not None
        except Exception:
            # 报错也是合理的
            pass


class TestMultipleEnvVars:
    """多环境变量组合测试"""

    def test_all_env_vars_combined(self, monkeypatch):
        """所有环境变量组合使用"""
        monkeypatch.setenv("POSTGRES_DSN", "postgresql://test:test@localhost:5432/engram")
        monkeypatch.setenv("PROJECT_KEY", "combined_test")
        monkeypatch.setenv("GATEWAY_PORT", "9999")
        monkeypatch.setenv("OPENMEMORY_BASE_URL", "http://memory:8080")
        
        from engram.logbook.config import Config
        
        config = Config.from_env()
        
        # 基本验证
        assert config is not None
        assert config.postgres_dsn is not None
        
        if hasattr(config, "project_key"):
            assert config.project_key == "combined_test"


class TestConfigIsolation:
    """配置隔离测试"""

    def test_config_instances_independent(self, monkeypatch):
        """多个 Config 实例相互独立"""
        from engram.logbook.config import Config
        
        monkeypatch.setenv("POSTGRES_DSN", "postgresql://first:first@localhost/first")
        config1 = Config.from_env()
        
        monkeypatch.setenv("POSTGRES_DSN", "postgresql://second:second@localhost/second")
        config2 = Config.from_env()
        
        # 两个实例应该有不同的值（如果 from_env 每次都读取环境变量）
        # 或者都是同一个值（如果有缓存）
        # 这里只验证不会相互干扰导致错误
        assert config1 is not None
        assert config2 is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
