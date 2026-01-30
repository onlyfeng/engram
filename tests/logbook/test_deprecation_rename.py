# -*- coding: utf-8 -*-
"""
重命名测试

验证从旧命名到 engram_logbook 重命名后的正确性：
1. 环境变量 ENGRAM_LOGBOOK_CONFIG 正确定义
2. 模块名称正确
3. 包导出可用
4. 配置文件路径优先级正确

相关文档:
- README.md: 配置文件（Logbook CLI）部分
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch

from engram.logbook.config import (
    Config,
    ENV_CONFIG_PATH,
)


class TestRenameDocumentation:
    """验证重命名相关文档一致性"""

    def test_env_var_names_documented(self):
        """
        验证环境变量名称常量正确定义
        
        文档中引用的环境变量名必须与代码一致。
        """
        from engram.logbook.config import ENV_CONFIG_PATH
        
        # 环境变量名
        assert ENV_CONFIG_PATH == "ENGRAM_LOGBOOK_CONFIG"

    def test_module_name_correct(self):
        """
        验证模块名称正确
        
        历史模块名/旧命名（已移除）已完全替换为 engram_logbook。
        """
        import engram_logbook
        
        assert engram_logbook.__name__ == "engram_logbook"
        assert hasattr(engram_logbook, "__version__")

    def test_package_exports_available(self):
        """
        验证包导出的核心类/函数可用
        
        确保重命名后所有公共 API 仍然可访问。
        """
        from engram.logbook import (
            Config,
            Database,
            get_config,
            get_database,
            ENV_CONFIG_PATH,
        )
        
        assert Config is not None
        assert Database is not None
        assert callable(get_config)
        assert callable(get_database)


class TestConfigPriorityDocumentation:
    """验证配置优先级与文档一致"""

    def test_priority_order(self, tmp_path: Path):
        """
        验证配置加载优先级顺序
        
        优先级（从高到低）：
        1. --config/-c 参数显式指定的路径
        2. ENGRAM_LOGBOOK_CONFIG 环境变量
        3. ./.agentx/config.toml
        4. ~/.agentx/config.toml
        """
        # 创建显式路径配置
        explicit_config = tmp_path / "explicit.toml"
        explicit_config.write_text("""
[postgres]
dsn = "postgresql://explicit:pass@localhost:5432/db"

[project]
project_key = "explicit"
""")
        
        # 创建环境变量配置
        env_config = tmp_path / "env.toml"
        env_config.write_text("""
[postgres]
dsn = "postgresql://env:pass@localhost:5432/db"

[project]
project_key = "env"
""")
        
        # 测试 1: 显式路径最优先
        with patch.dict(os.environ, {ENV_CONFIG_PATH: str(env_config)}):
            config = Config(config_path=str(explicit_config))
            config.load()
            assert config.get("project.project_key") == "explicit"
        
        # 测试 2: 环境变量次优先
        with patch.dict(os.environ, {ENV_CONFIG_PATH: str(env_config)}):
            config = Config()  # 不指定显式路径
            config.load()
            assert config.get("project.project_key") == "env"


class TestCLIHelpText:
    """验证 CLI 帮助文本正确显示环境变量"""

    def test_add_config_argument_help_mentions_env_var(self):
        """
        --config 参数的帮助文本应提及环境变量
        """
        import argparse
        from engram.logbook.config import add_config_argument
        
        parser = argparse.ArgumentParser()
        add_config_argument(parser)
        
        # 获取帮助文本
        help_text = parser.format_help()
        
        # 应包含环境变量
        assert "ENGRAM_LOGBOOK_CONFIG" in help_text
        # 应包含默认路径
        assert ".agentx/config.toml" in help_text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
