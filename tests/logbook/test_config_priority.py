# -*- coding: utf-8 -*-
"""
测试配置加载优先级和缺失报错

优先级（从高到低）：
1. --config/-c 参数显式指定的路径
2. 环境变量 ENGRAM_LOGBOOK_CONFIG
3. ./.agentx/config.toml（当前目录）
4. ~/.agentx/config.toml（用户家目录）

敏感凭证约束：
- S3/MinIO 凭证只能通过环境变量配置，不能写入配置文件
- GitLab token 优先使用环境变量或 exec 命令获取
"""

import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile
import shutil

from engram.logbook.config import (
    Config,
    get_config,
    get_app_config,
    add_config_argument,
    init_config_from_args,
    ENV_CONFIG_PATH,
    DEFAULT_CONFIG_PATHS,
    get_gitlab_auth,
    get_svn_auth,
)
from engram.logbook.errors import ConfigError, ConfigNotFoundError


class TestConfigPriority:
    """测试配置文件加载优先级"""

    def test_priority_explicit_path_highest(self, tmp_path: Path):
        """
        显式路径优先级最高
        
        即使环境变量和默认路径都存在，显式路径也应该被使用。
        """
        # 创建显式配置文件
        explicit_config = tmp_path / "explicit.toml"
        explicit_config.write_text("""
[postgres]
dsn = "postgresql://explicit:pass@host:5432/db"

[project]
project_key = "explicit_project"
""")
        
        # 创建环境变量指向的配置文件
        env_config = tmp_path / "env.toml"
        env_config.write_text("""
[postgres]
dsn = "postgresql://env:pass@host:5432/db"

[project]
project_key = "env_project"
""")
        
        # 创建当前目录配置文件
        cwd_config_dir = tmp_path / ".agentx"
        cwd_config_dir.mkdir()
        cwd_config = cwd_config_dir / "config.toml"
        cwd_config.write_text("""
[postgres]
dsn = "postgresql://cwd:pass@host:5432/db"

[project]
project_key = "cwd_project"
""")
        
        # 同时设置环境变量
        with patch.dict(os.environ, {ENV_CONFIG_PATH: str(env_config)}):
            with patch.object(Path, 'cwd', return_value=tmp_path):
                config = Config(config_path=str(explicit_config))
                config.load()
                
                assert config.get("postgres.dsn") == "postgresql://explicit:pass@host:5432/db"
                assert config.get("project.project_key") == "explicit_project"
                assert config.config_path == explicit_config

    def test_priority_env_over_default(self, tmp_path: Path):
        """
        环境变量优先级高于默认路径
        
        当 --config 未指定时，ENGRAM_LOGBOOK_CONFIG 优先于默认路径。
        """
        # 创建环境变量指向的配置文件
        env_config = tmp_path / "env.toml"
        env_config.write_text("""
[postgres]
dsn = "postgresql://env:pass@host:5432/db"

[project]
project_key = "env_project"
""")
        
        # 创建当前目录配置文件
        cwd_config_dir = tmp_path / ".agentx"
        cwd_config_dir.mkdir()
        cwd_config = cwd_config_dir / "config.toml"
        cwd_config.write_text("""
[postgres]
dsn = "postgresql://cwd:pass@host:5432/db"

[project]
project_key = "cwd_project"
""")
        
        with patch.dict(os.environ, {ENV_CONFIG_PATH: str(env_config)}):
            config = Config()  # 不指定显式路径
            config.load()
            
            assert config.get("project.project_key") == "env_project"
            assert config.config_path == env_config

    def test_priority_cwd_over_home(self, tmp_path: Path, monkeypatch):
        """
        当前目录配置优先于家目录配置
        
        ./.agentx/config.toml 优先于 ~/.agentx/config.toml
        """
        # 创建当前目录配置文件
        cwd_config_dir = tmp_path / "cwd" / ".agentx"
        cwd_config_dir.mkdir(parents=True)
        cwd_config = cwd_config_dir / "config.toml"
        cwd_config.write_text("""
[postgres]
dsn = "postgresql://cwd:pass@host:5432/db"

[project]
project_key = "cwd_project"
""")
        
        # 创建家目录配置文件
        home_config_dir = tmp_path / "home" / ".agentx"
        home_config_dir.mkdir(parents=True)
        home_config = home_config_dir / "config.toml"
        home_config.write_text("""
[postgres]
dsn = "postgresql://home:pass@host:5432/db"

[project]
project_key = "home_project"
""")
        
        # Mock 掉默认路径
        cwd_path = Path("./.agentx/config.toml")
        home_path = tmp_path / "home" / ".agentx" / "config.toml"
        
        # 确保环境变量未设置
        env = {k: v for k, v in os.environ.items() if k != ENV_CONFIG_PATH}
        
        with patch.dict(os.environ, env, clear=True):
            # Mock DEFAULT_CONFIG_PATHS
            with patch('engram_logbook.config.DEFAULT_CONFIG_PATHS', [cwd_config, home_config]):
                config = Config()
                config.load()
                
                assert config.get("project.project_key") == "cwd_project"
                assert config.config_path == cwd_config

    def test_home_fallback_when_cwd_missing(self, tmp_path: Path, monkeypatch):
        """
        当前目录配置不存在时回退到家目录
        """
        # 只创建家目录配置文件
        home_config_dir = tmp_path / "home" / ".agentx"
        home_config_dir.mkdir(parents=True)
        home_config = home_config_dir / "config.toml"
        home_config.write_text("""
[postgres]
dsn = "postgresql://home:pass@host:5432/db"

[project]
project_key = "home_project"
""")
        
        # 不存在的当前目录配置
        cwd_config = tmp_path / "nonexistent" / ".agentx" / "config.toml"
        
        env = {k: v for k, v in os.environ.items() if k != ENV_CONFIG_PATH}
        
        with patch.dict(os.environ, env, clear=True):
            with patch('engram_logbook.config.DEFAULT_CONFIG_PATHS', [cwd_config, home_config]):
                config = Config()
                config.load()
                
                assert config.get("project.project_key") == "home_project"
                assert config.config_path == home_config


class TestConfigNotFoundError:
    """测试配置文件缺失时的报错"""

    def test_explicit_path_not_found_raises_error(self, tmp_path: Path):
        """
        显式指定的配置文件不存在时抛出 ConfigNotFoundError
        """
        nonexistent = tmp_path / "nonexistent.toml"
        
        with pytest.raises(ConfigNotFoundError) as exc_info:
            Config(config_path=str(nonexistent))
        
        error = exc_info.value
        assert "指定的配置文件不存在" in error.message
        assert str(nonexistent.absolute()) in str(error.details)

    def test_env_path_not_found_raises_error(self, tmp_path: Path):
        """
        环境变量指定的配置文件不存在时抛出 ConfigNotFoundError
        """
        nonexistent = tmp_path / "env_nonexistent.toml"
        
        with patch.dict(os.environ, {ENV_CONFIG_PATH: str(nonexistent)}):
            with pytest.raises(ConfigNotFoundError) as exc_info:
                Config()
            
            error = exc_info.value
            assert ENV_CONFIG_PATH in error.message
            assert "env_var" in error.details

    def test_all_paths_missing_allows_empty_config(self, tmp_path: Path):
        """
        所有默认路径都不存在时，允许使用空配置
        
        这允许仅使用环境变量配置的场景。
        """
        nonexistent_cwd = tmp_path / "cwd" / ".agentx" / "config.toml"
        nonexistent_home = tmp_path / "home" / ".agentx" / "config.toml"
        
        env = {k: v for k, v in os.environ.items() if k != ENV_CONFIG_PATH}
        
        with patch.dict(os.environ, env, clear=True):
            with patch('engram_logbook.config.DEFAULT_CONFIG_PATHS', [nonexistent_cwd, nonexistent_home]):
                config = Config()
                config.load()
                
                assert config.config_path is None
                assert config.data == {}


class TestConfigMissingHint:
    """测试配置缺失时的修复提示"""

    def test_app_config_missing_postgres_hint(self, tmp_path: Path):
        """
        缺少 [postgres] 配置节时提供明确的修复提示
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[project]
project_key = "test"
""")
        
        config = Config(config_path=str(config_file))
        config.load()
        
        with pytest.raises(ConfigError) as exc_info:
            config.to_app_config()
        
        error = exc_info.value
        assert "postgres" in error.message.lower()
        assert "section" in str(error.details)

    def test_app_config_missing_project_hint(self, tmp_path: Path):
        """
        缺少 [project] 配置节时提供明确的修复提示
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[postgres]
dsn = "postgresql://user:pass@host:5432/db"
""")
        
        config = Config(config_path=str(config_file))
        config.load()
        
        with pytest.raises(ConfigError) as exc_info:
            config.to_app_config()
        
        error = exc_info.value
        assert "project" in error.message.lower()

    def test_app_config_missing_dsn_hint(self, tmp_path: Path):
        """
        缺少 postgres.dsn 配置时提供明确的修复提示
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[postgres]
pool_min_size = 1

[project]
project_key = "test"
""")
        
        config = Config(config_path=str(config_file))
        config.load()
        
        with pytest.raises(ConfigError) as exc_info:
            config.to_app_config()
        
        error = exc_info.value
        assert "dsn" in error.message.lower()


class TestSensitiveCredentialsConstraint:
    """测试敏感凭证只走环境变量的约束"""

    def test_s3_credentials_from_env_only(self, tmp_path: Path, monkeypatch):
        """
        S3/MinIO 凭证只能通过环境变量获取
        
        配置文件中不应该存储 access_key 和 secret_key。
        ArtifactsObjectConfig 的文档明确说明敏感凭证必须通过环境变量注入。
        """
        # 验证 ArtifactsObjectConfig 没有 access_key/secret_key 字段
        from engram.logbook.config import ArtifactsObjectConfig
        
        # 创建默认实例
        obj_config = ArtifactsObjectConfig()
        
        # 确认没有敏感凭证字段
        assert not hasattr(obj_config, 'access_key')
        assert not hasattr(obj_config, 'secret_key')
        assert not hasattr(obj_config, 'endpoint')  # endpoint 也应该从环境变量获取
        assert not hasattr(obj_config, 'bucket')  # bucket 也应该从环境变量获取
        
        # 验证文档中列出的环境变量
        # ENGRAM_S3_ENDPOINT, ENGRAM_S3_ACCESS_KEY, ENGRAM_S3_SECRET_KEY, ENGRAM_S3_BUCKET

    def test_gitlab_token_not_in_config_recommended(self, tmp_path: Path):
        """
        GitLab token 推荐通过环境变量或 exec 命令获取
        
        虽然旧配置支持 gitlab.private_token，但新配置推荐使用：
        1. scm.gitlab.auth.token_env（环境变量名）
        2. scm.gitlab.auth.token_file（文件路径）
        3. scm.gitlab.auth.exec（命令获取）
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[postgres]
dsn = "postgresql://user:pass@host:5432/db"

[project]
project_key = "test"

[scm.gitlab]
url = "https://gitlab.example.com"
project = "group/repo"

[scm.gitlab.auth]
mode = "pat"
token_env = "MY_GITLAB_TOKEN"  # 推荐：指定环境变量名
""")
        
        # 设置环境变量
        with patch.dict(os.environ, {"MY_GITLAB_TOKEN": "glpat-xxxx"}):
            from engram.logbook.config import get_config
            config = Config(config_path=str(config_file))
            config.load()
            
            # 重置全局配置
            import engram_logbook.config as cfg_module
            cfg_module._global_config = config
            cfg_module._global_app_config = None
            
            auth = get_gitlab_auth(config)
            
            assert auth is not None
            assert auth.token == "glpat-xxxx"
            assert auth.source == "env:MY_GITLAB_TOKEN"

    def test_svn_password_from_env_only(self, tmp_path: Path):
        """
        SVN 密码只能通过环境变量或文件获取
        
        配置文件中只能指定 password_env（环境变量名）或 password_file（文件路径）。
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[postgres]
dsn = "postgresql://user:pass@host:5432/db"

[project]
project_key = "test"

[scm.svn]
url = "svn://svn.example.com/repo"
username = "svn_user"
password_env = "MY_SVN_PASSWORD"  # 指定环境变量名，不直接写密码
""")
        
        with patch.dict(os.environ, {"MY_SVN_PASSWORD": "svn_secret"}):
            from engram.logbook.config import get_config
            config = Config(config_path=str(config_file))
            config.load()
            
            # 重置全局配置
            import engram_logbook.config as cfg_module
            cfg_module._global_config = config
            cfg_module._global_app_config = None
            
            auth = get_svn_auth(config)
            
            assert auth is not None
            assert auth.password == "svn_secret"
            assert "password:env:MY_SVN_PASSWORD" in auth.source


class TestAddConfigArgument:
    """测试 CLI 参数添加"""

    def test_add_config_argument_to_parser(self):
        """
        add_config_argument 正确添加 --config/-c 参数
        """
        import argparse
        parser = argparse.ArgumentParser()
        add_config_argument(parser)
        
        # 解析 --config
        args = parser.parse_args(["--config", "/path/to/config.toml"])
        assert args.config_path == "/path/to/config.toml"
        
        # 解析 -c
        args = parser.parse_args(["-c", "/path/to/config.toml"])
        assert args.config_path == "/path/to/config.toml"
        
        # 不指定时为 None
        args = parser.parse_args([])
        assert args.config_path is None


class TestInitConfigFromArgs:
    """测试 CLI 参数初始化配置"""

    def test_init_config_from_args_with_path(self, tmp_path: Path):
        """
        init_config_from_args 使用 args.config_path 初始化配置
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[postgres]
dsn = "postgresql://user:pass@host:5432/db"

[project]
project_key = "test_project"
""")
        
        # Mock args 对象
        args = MagicMock()
        args.config_path = str(config_file)
        
        app_config = init_config_from_args(args)
        
        assert app_config.project.project_key == "test_project"

    def test_init_config_from_args_without_path(self, tmp_path: Path):
        """
        init_config_from_args 在无 config_path 时使用默认路径
        """
        # 创建默认路径配置
        default_dir = tmp_path / ".agentx"
        default_dir.mkdir()
        config_file = default_dir / "config.toml"
        config_file.write_text("""
[postgres]
dsn = "postgresql://user:pass@host:5432/db"

[project]
project_key = "default_project"
""")
        
        # Mock args 对象（没有 config_path 或为 None）
        args = MagicMock()
        args.config_path = None
        
        env = {k: v for k, v in os.environ.items() if k != ENV_CONFIG_PATH}
        
        with patch.dict(os.environ, env, clear=True):
            with patch('engram_logbook.config.DEFAULT_CONFIG_PATHS', [config_file]):
                app_config = init_config_from_args(args)
                
                assert app_config.project.project_key == "default_project"


class TestConfigErrorHintMessage:
    """测试配置错误的 hint 消息"""

    def test_config_not_found_error_has_hint(self, tmp_path: Path):
        """
        ConfigNotFoundError 应该包含修复提示
        """
        nonexistent = tmp_path / "nonexistent.toml"
        
        with pytest.raises(ConfigNotFoundError) as exc_info:
            Config(config_path=str(nonexistent))
        
        error = exc_info.value
        # 错误信息应该包含路径
        assert str(nonexistent) in error.message or str(nonexistent.absolute()) in str(error.details)

    def test_missing_section_error_has_hint(self, tmp_path: Path):
        """
        缺少必需配置节时应该有修复提示
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
# 空配置
""")
        
        config = Config(config_path=str(config_file))
        config.load()
        
        with pytest.raises(ConfigError) as exc_info:
            config.to_app_config()
        
        error = exc_info.value
        assert "section" in str(error.details) or "postgres" in error.message.lower()


class TestEnvironmentVariableOverrides:
    """测试环境变量覆盖配置"""

    def test_admin_dsn_env_override(self, tmp_path: Path):
        """
        ENGRAM_PG_ADMIN_DSN 环境变量覆盖配置文件中的 admin_dsn
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[postgres]
dsn = "postgresql://user:pass@host:5432/db"
admin_dsn = "postgresql://config_admin:pass@host:5432/postgres"

[project]
project_key = "test"
""")
        
        env_admin_dsn = "postgresql://env_admin:pass@host:5432/postgres"
        
        with patch.dict(os.environ, {"ENGRAM_PG_ADMIN_DSN": env_admin_dsn}):
            config = Config(config_path=str(config_file))
            app_config = config.to_app_config()
            
            assert app_config.postgres.admin_dsn == env_admin_dsn

    def test_artifacts_read_only_env_override(self, tmp_path: Path):
        """
        ENGRAM_ARTIFACTS_READ_ONLY 环境变量覆盖配置文件
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[postgres]
dsn = "postgresql://user:pass@host:5432/db"

[project]
project_key = "test"

[artifacts.policy]
read_only = false
""")
        
        # 环境变量设置为 true
        with patch.dict(os.environ, {"ENGRAM_ARTIFACTS_READ_ONLY": "true"}):
            config = Config(config_path=str(config_file))
            app_config = config.to_app_config()
            
            assert app_config.artifacts.policy.read_only is True

    def test_artifacts_read_only_config_fallback(self, tmp_path: Path):
        """
        环境变量未设置时使用配置文件的值
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[postgres]
dsn = "postgresql://user:pass@host:5432/db"

[project]
project_key = "test"

[artifacts.policy]
read_only = true
""")
        
        # 确保环境变量未设置
        env = {k: v for k, v in os.environ.items() if k != "ENGRAM_ARTIFACTS_READ_ONLY"}
        
        with patch.dict(os.environ, env, clear=True):
            config = Config(config_path=str(config_file))
            app_config = config.to_app_config()
            
            assert app_config.artifacts.policy.read_only is True
